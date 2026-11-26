"""The workflow shared by the CLI and the MCP server: create, verify and apply plans."""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, load_sources
from .errors import PlanError, ProviderError
from .plan import GATES, PLAN_FORMAT, body, counts, fingerprint, plan_hash
from .providers import Backend, get_provider

DATA_DIR = ".adstack"
RECEIPT_FORMAT = "adstack-receipt/1"


@dataclass(frozen=True)
class Workspace:
    """Where Adstack keeps local files (``.adstack/``) and whether it talks to the sandbox."""

    root: Path = field(default_factory=Path.cwd)
    sandbox: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    @property
    def data_dir(self) -> Path:
        return Path(self.root) / DATA_DIR


def connect(config: Config, workspace: Workspace) -> Backend:
    provider = get_provider(config.provider)
    sandbox_dir = workspace.data_dir / "sandbox" if workspace.sandbox else None
    return provider.connect(config.desired, sandbox_dir=sandbox_dir, env=workspace.env)


def target_label(provider_name: str, backend: Backend) -> str:
    provider = get_provider(provider_name)
    kind = "sandbox account" if backend.sandbox else "account"
    return f"{provider.title} {kind} {provider.format_account(backend.account)}"


def create_plan(
    config: Config,
    workspace: Workspace,
    *,
    prune: bool = False,
    destroy: bool = False,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Read the account and compute a plan. Nothing in the account changes."""
    if prune and destroy:
        raise PlanError("--prune and --destroy cannot be combined")
    provider = get_provider(config.provider)
    backend = backend or connect(config, workspace)
    state = backend.read()
    result = provider.plan(config.desired, state, backend.account, prune=prune, destroy=destroy)
    return {
        "format": PLAN_FORMAT,
        "adstack_version": __version__,
        "created_at": _now(),
        "provider": config.provider,
        "backend": "sandbox" if backend.sandbox else "api",
        "account": backend.account,
        "target": target_label(config.provider, backend),
        "options": {"prune": prune, "destroy": destroy},
        "config": {"files": dict(sorted(config.sources.items())), "summary": config.summary()},
        "config_hash": fingerprint(config.desired),
        "state_hash": fingerprint(state),
        "summary": counts(result["changes"]),
        "changes": result["changes"],
        "blockers": result["blockers"],
        "requires": result["requires"],
        "operations": result["operations"],
    }


def verify(plan: dict[str, Any], workspace: Workspace) -> tuple[dict[str, Any], Backend]:
    """Recompute a saved plan from its embedded configuration and the account's current state.

    Raises PlanError unless the result is identical, so a plan can only be applied
    while it still describes exactly what will happen.
    """
    if plan.get("format") != PLAN_FORMAT:
        raise PlanError("not an Adstack plan (or one from an incompatible version)")
    files = plan.get("config", {}).get("files") if isinstance(plan.get("config"), dict) else None
    if not isinstance(files, dict):
        raise PlanError("the plan does not contain its configuration")
    config = load_sources(files)
    if config.provider != plan.get("provider") or fingerprint(config.desired) != plan.get("config_hash"):
        raise PlanError("the plan's embedded configuration does not match its hash; the plan file was modified")
    workspace = replace(workspace, sandbox=plan.get("backend") == "sandbox")
    backend = connect(config, workspace)
    if backend.account != plan.get("account"):
        raise PlanError(
            f"this plan targets account {plan.get('account')}, but the configuration and environment now "
            f"target {backend.account}"
        )
    options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    fresh = create_plan(
        config,
        workspace,
        prune=bool(options.get("prune")),
        destroy=bool(options.get("destroy")),
        backend=backend,
    )
    if body(fresh) != body(plan):
        raise PlanError(
            "the account or the configuration changed after this plan was created; create a new plan and review it"
        )
    return fresh, backend


def missing_permissions(plan: dict[str, Any], allow: Collection[str]) -> list[str]:
    return [gate for gate in GATES if plan["requires"].get(gate) and gate not in allow]


def apply(
    plan: dict[str, Any],
    workspace: Workspace,
    *,
    allow: Collection[str] = (),
    verified: tuple[dict[str, Any], Backend] | None = None,
) -> tuple[dict[str, Any], Path] | None:
    """Apply a reviewed plan after verifying it is still current.

    The platform validates the whole request first; then it is applied atomically and a
    receipt is written to ``.adstack/receipts/``. Returns ``(receipt, path)``, or None
    when there is nothing to do.
    """
    fresh, backend = verified if verified is not None else verify(plan, workspace)
    if fresh["blockers"]:
        raise PlanError("this plan has blockers and cannot be applied: " + "; ".join(fresh["blockers"]))
    if not fresh["operations"]:
        return None
    missing = missing_permissions(fresh, allow)
    if missing:
        needed = ", ".join(f"{gate} ({GATES[gate]})" for gate in missing)
        raise PlanError(f"applying this plan needs explicit permission: {needed}")
    receipts = workspace.data_dir / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    backend.mutate(fresh["operations"], validate_only=True)
    response = backend.mutate(fresh["operations"], validate_only=False)
    digest = plan_hash(fresh)
    receipt = {
        "format": RECEIPT_FORMAT,
        "applied_at": _now(),
        "adstack_version": __version__,
        "provider": fresh["provider"],
        "backend": fresh["backend"],
        "account": fresh["account"],
        "target": fresh["target"],
        "plan_sha256": digest,
        "summary": fresh["summary"],
        "changes": fresh["changes"],
        "response": response,
    }
    path = receipts / f"{_now(compact=True)}-{digest[:12]}.json"
    try:
        with path.open("x", encoding="utf-8") as file:
            json.dump(receipt, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except OSError as exc:
        raise ProviderError(
            f"The changes were applied, but writing the receipt to {path} failed ({exc}). "
            "Run plan before making further changes."
        ) from exc
    return receipt, path


def _now(*, compact: bool = False) -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ") if compact else now.strftime("%Y-%m-%dT%H:%M:%SZ")
