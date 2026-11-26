"""Model Context Protocol server: AI assistants can validate, plan and (optionally) apply configurations.

Run it with ``adstack mcp``. Tools accept a configuration path relative to the
server's root directory, or configuration text. Planning reads the ad account
and changes nothing. The ``apply`` tool exists only when the server uses the
sandbox or was started with ``--allow-apply``, and even then it accepts only a
plan the server created, re-verifies it against the live account, requires the
account ID as confirmation and the same explicit permissions as the CLI.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, engine
from . import plan as plans
from .config import Config, load, load_sources
from .errors import AdstackError, ConfigError, PlanError
from .providers import get_provider
from .scaffold import STARTER_CONFIG

_PLAN_ID = re.compile(r"[0-9a-f]{16}")

_WORKFLOW = """\
Adstack manages advertising campaigns as code. A YAML configuration declares the campaigns; Adstack \
compares it with the ad account and plans the changes, like Terraform.

Workflow:
1. Write or edit a configuration. Call example_config for an annotated example of every option. \
validate checks a configuration offline and reports the exact problem.
2. Call plan to see what would change. Planning reads the account but changes nothing.
3. Show the user plan_text (every change, blocker and required permission) and ask whether to apply it.
"""

_APPLY_RULES = """\
4. Only after the user explicitly approves that specific plan, call apply with its plan_id and the account ID.

Rules:
- Never call apply without the user's explicit approval of the plan, and never reuse an old approval.
- Set allow_enable, allow_budget_increase or allow_live_changes only when the plan requires it and the user \
approved that effect.
- If apply reports that the account changed, plan again and ask for approval again.
"""

_NO_APPLY = """\
This server cannot apply changes. To apply a plan, the user can run 'adstack plan --out FILE' and \
'adstack apply FILE' in a terminal, or restart this server with --allow-apply.
"""

_NOTES = """\
Notes: new campaigns are always created PAUSED, and enabling one is a separate plan. Amounts are in the \
account currency. daily_budget is the platform's average daily budget, not a hard cap.
"""


def instructions(*, sandbox: bool, can_apply: bool) -> str:
    mode = (
        "This server uses a local sandbox account: nothing reaches a real ad platform.\n"
        if sandbox
        else "This server is connected to the real ad platform: applied changes spend real money.\n"
    )
    return _WORKFLOW + (_APPLY_RULES if can_apply else _NO_APPLY) + _NOTES + mode


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Report Adstack's expected errors to the client with their message."""
    try:
        yield
    except AdstackError as exc:
        raise ToolError(str(exc)) from None


def _inside(root: Path, path: str) -> Path:
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"{path!r} is outside the server's root directory")
    return candidate


def create_server(
    root: str | Path,
    *,
    sandbox: bool = False,
    allow_apply: bool = False,
    env: Mapping[str, str] | None = None,
) -> MCPServer:
    root = Path(root).resolve()
    workspace = engine.Workspace(root=root, sandbox=sandbox, env=dict(os.environ) if env is None else dict(env))
    plans_dir = workspace.data_dir / "plans"
    can_apply = allow_apply or sandbox
    server = MCPServer(
        "adstack",
        title="Adstack",
        description="Advertising campaigns as code: validate, plan and apply ad configurations.",
        instructions=instructions(sandbox=sandbox, can_apply=can_apply),
        version=__version__,
    )

    def read_config(path: str | None, config: str | None) -> Config:
        if config is not None:
            if path:
                raise ToolError("pass either path or config, not both")
            return load_sources({"config.yaml": config})
        return load(_inside(root, path or "."))

    def store(plan: dict[str, Any]) -> str:
        plan_id = plans.plan_hash(plan)[:16]
        target = plans_dir / f"{plan_id}.json"
        if not target.exists():
            plans_dir.mkdir(parents=True, exist_ok=True)
            with suppress(PlanError):  # saved concurrently: identical content
                plans.save(plan, target)
        return plan_id

    def stored(plan_id: str) -> dict[str, Any]:
        if not _PLAN_ID.fullmatch(plan_id):
            raise ToolError("plan_id must be the 16-character ID returned by the plan tool")
        target = plans_dir / f"{plan_id}.json"
        if not target.is_file():
            raise ToolError(f"no plan with ID {plan_id}; call plan to create one")
        return plans.load(target)

    def describe(plan: dict[str, Any], plan_id: str | None) -> dict[str, Any]:
        required = [gate for gate, needed in plan["requires"].items() if needed]
        if plan["blockers"]:
            next_step = (
                "This plan is blocked and cannot be applied. Resolve the blockers or plan with prune=true "
                "where suggested."
            )
        elif not plan["operations"]:
            next_step = "Nothing to do: the account matches the configuration."
        elif can_apply:
            permissions = f" with {', '.join(f'{gate}=true' for gate in required)}" if required else ""
            next_step = (
                "Show plan_text to the user and ask for approval. Only if they explicitly approve, call apply with "
                f"plan_id={plan_id} and confirm_account={plan['account']}{permissions}."
            )
        else:
            next_step = "Show plan_text to the user. " + _NO_APPLY.strip()
        return {
            "plan_id": plan_id,
            "target": plan["target"],
            "account": plan["account"],
            "sandbox": plan["backend"] == "sandbox",
            "summary": plan["summary"],
            "has_changes": bool(plan["operations"]),
            "blockers": plan["blockers"],
            "requires": required,
            "plan_text": plans.render(plan),
            "changes": plan["changes"],
            "next_step": next_step,
        }

    @server.tool(
        annotations=ToolAnnotations(
            title="Validate a configuration", readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    def validate(path: str | None = None, config: str | None = None) -> dict[str, Any]:
        """Check an Adstack configuration offline, without contacting the ad platform.

        Pass `path` (a YAML file or a directory of YAML files, relative to the server's root;
        default: the root) or `config` (YAML text). Returns valid=true with a summary, or
        valid=false with the exact problem.
        """
        try:
            loaded = read_config(path, config)
        except ConfigError as exc:
            return {"valid": False, "error": str(exc)}
        except AdstackError as exc:
            raise ToolError(str(exc)) from None
        return {"valid": True, "provider": loaded.provider, "summary": loaded.summary()}

    @server.tool(
        annotations=ToolAnnotations(
            title="Plan changes", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )
    def plan(
        path: str | None = None, config: str | None = None, prune: bool = False, destroy: bool = False
    ) -> dict[str, Any]:
        """Read the ad account and show what applying the configuration would change. Changes nothing.

        Pass `path` or `config` as for validate. `prune` also pauses or removes managed resources
        that are no longer in the configuration; `destroy` plans to pause every managed campaign.
        Show the returned plan_text to the user. A plan without blockers is saved; its plan_id
        is what apply needs.
        """
        with _tool_errors():
            created = engine.create_plan(read_config(path, config), workspace, prune=prune, destroy=destroy)
        plan_id = store(created) if created["operations"] and not created["blockers"] else None
        return describe(created, plan_id)

    @server.tool(
        annotations=ToolAnnotations(
            title="Show a saved plan", readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    def show_plan(plan_id: str) -> dict[str, Any]:
        """Show a plan created earlier by the plan tool, including when it was created."""
        with _tool_errors():
            saved = stored(plan_id)
        return {**describe(saved, plan_id), "created_at": saved.get("created_at")}

    if can_apply:

        @server.tool(
            annotations=ToolAnnotations(
                title="Apply an approved plan",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=True,
            )
        )
        def apply(
            plan_id: str,
            confirm_account: str,
            allow_enable: bool = False,
            allow_budget_increase: bool = False,
            allow_live_changes: bool = False,
        ) -> dict[str, Any]:
            """Apply a plan from the plan tool. Call this only after the user explicitly approved that plan.

            `confirm_account` must be the plan's account ID. Set an allow_* flag only if the plan
            requires it and the user approved that effect: allow_enable (a paused campaign starts
            serving), allow_budget_increase (a budget or bid increases), allow_live_changes (a campaign
            that is serving changes). Adstack re-reads the account first and refuses the plan if anything
            changed since it was created.
            """
            with _tool_errors():
                saved = stored(plan_id)
                if re.sub(r"[^0-9]", "", confirm_account) != saved["account"]:
                    raise ToolError(f"confirm_account does not match the plan's account ({saved['target']})")
                allow = {
                    gate
                    for gate, granted in (
                        ("allow_enable", allow_enable),
                        ("allow_budget_increase", allow_budget_increase),
                        ("allow_live_changes", allow_live_changes),
                    )
                    if granted
                }
                result = engine.apply(saved, workspace, allow=allow)
            if result is None:
                return {"applied": False, "message": "Nothing to apply: the account already matches the plan."}
            receipt, path = result
            return {
                "applied": True,
                "target": receipt["target"],
                "summary": receipt["summary"],
                "receipt": str(path.relative_to(root)),
            }

    @server.tool(
        annotations=ToolAnnotations(
            title="List managed resources", readOnlyHint=True, idempotentHint=True, openWorldHint=True
        )
    )
    def state(path: str | None = None, config: str | None = None) -> dict[str, Any]:
        """List what Adstack manages in the account: campaigns, ad groups, keywords and ads with their statuses."""
        with _tool_errors():
            loaded = read_config(path, config)
            backend = engine.connect(loaded, workspace)
            records = get_provider(loaded.provider).inventory(loaded.desired, backend.read())
        return {"target": engine.target_label(loaded.provider, backend), "resources": records}

    @server.tool(
        annotations=ToolAnnotations(
            title="Performance report", readOnlyHint=True, idempotentHint=True, openWorldHint=True
        )
    )
    def report(
        kind: Literal["performance", "search_terms"] = "performance",
        days: Literal[7, 14, 30] = 7,
        path: str | None = None,
        config: str | None = None,
        all_campaigns: bool = False,
    ) -> dict[str, Any]:
        """Campaign performance or search terms for the last 7, 14 or 30 days, with cost in account currency.

        By default only campaigns in the configuration's namespace are included.
        """
        with _tool_errors():
            loaded = read_config(path, config)
            backend = engine.connect(loaded, workspace)
            rows = backend.report(kind, days, all_campaigns=all_campaigns)
        return {
            "target": engine.target_label(loaded.provider, backend),
            "kind": kind,
            "days": days,
            "currency": loaded.desired.get("currency"),
            "rows": rows,
        }

    @server.tool(
        annotations=ToolAnnotations(
            title="Example configuration", readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    def example_config() -> dict[str, Any]:
        """An annotated example configuration that documents every option."""
        return {"config": STARTER_CONFIG}

    @server.resource(
        "adstack://docs/example-config",
        name="example-config",
        title="Annotated example configuration",
        description="An Adstack configuration that documents every option.",
        mime_type="text/yaml",
    )
    def example_config_resource() -> str:
        return STARTER_CONFIG

    return server


def serve(root: Path, *, sandbox: bool, allow_apply: bool) -> None:
    """Run the server over stdio."""
    create_server(root, sandbox=sandbox, allow_apply=allow_apply).run("stdio")
