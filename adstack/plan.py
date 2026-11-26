"""Plan documents: hashing, plan files, and Terraform-style rendering.

A plan is a JSON document. It embeds the configuration it was made from, a hash
of the account state it was made against, the reviewed changes and the exact
API operations. Applying a plan recomputes all of it from the embedded
configuration and the account's current state, and refuses any difference.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import PlanError

PLAN_FORMAT = "adstack-plan/1"
METADATA = ("adstack_version", "created_at")
"""Keys that describe when and how a plan was made; they are not compared when verifying it."""

GATES = {
    "allow_enable": "a paused campaign will start serving ads",
    "allow_budget_increase": "a budget or bid on an existing resource will increase",
    "allow_live_changes": "a campaign that is serving now (ENABLED) will change",
}
"""Permissions a plan can require, in addition to approving the plan."""

_SYMBOL = {"create": "+", "update": "~", "remove": "-"}
_COLOR = {"create": "32", "update": "33", "remove": "31"}
_TITLE = {
    "campaign": "campaign",
    "budget": "daily budget",
    "location": "location",
    "negative_keyword": "negative keyword",
    "ad_group": "ad group",
    "keyword": "keyword",
    "ad": "",
}


def fingerprint(value: Any) -> str:
    """SHA-256 of canonical JSON."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def body(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in METADATA}


def plan_hash(plan: dict[str, Any]) -> str:
    return fingerprint(body(plan))


def counts(changes: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "add": sum(change["action"] == "create" for change in changes),
        "change": sum(change["action"] == "update" for change in changes),
        "remove": sum(change["action"] == "remove" for change in changes),
    }


def save(plan: dict[str, Any], path: str | Path) -> None:
    """Write a plan file. Existing files are never overwritten."""
    try:
        with Path(path).open("x", encoding="utf-8") as file:
            json.dump(plan, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except FileExistsError:
        raise PlanError(f"{path} already exists; plan files are never overwritten, choose a new name") from None


def load(path: str | Path) -> dict[str, Any]:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path}: cannot read the plan file ({exc})") from None
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        raise PlanError(f"{path}: not an Adstack plan file (or one from an incompatible version)")
    return plan


def is_plan_file(path: str | Path) -> bool:
    path = Path(path)
    if not path.is_file() or path.suffix in (".yaml", ".yml"):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("format") == PLAN_FORMAT


def summary_line(plan: dict[str, Any]) -> str:
    total = plan["summary"]
    return f"Plan: {total['add']} to add, {total['change']} to change, {total['remove']} to remove."


def render(plan: dict[str, Any], *, color: bool = False) -> str:
    """Human-readable plan, in the spirit of ``terraform plan``."""
    lines: list[str] = []
    if plan["changes"]:
        lines += [f"Adstack will perform the following actions in {plan['target']}:", ""]
        lines += render_changes(plan["changes"], color=color)
        lines.append("")
    if plan["blockers"]:
        lines.append("Blocked: this plan cannot be applied until the following are resolved:")
        lines += [f"  - {blocker}" for blocker in plan["blockers"]]
        lines.append("")
    required = [gate for gate, needed in plan["requires"].items() if needed]
    if required:
        lines.append("Applying this plan also requires explicit permission:")
        lines += [f"  --{gate.replace('_', '-'):<24} {GATES.get(gate, '')}" for gate in required]
        lines.append("")
    if plan["operations"]:
        lines.append(_paint(summary_line(plan), "1", color))
    elif not plan["blockers"]:
        lines.append(f"No changes. The {plan['target']} matches the configuration.")
    return "\n".join(lines).rstrip("\n")


def render_changes(changes: list[dict[str, Any]], *, color: bool = False) -> list[str]:
    lines: list[str] = []
    shown_campaign: str | None = None
    shown_group: str | None = None
    for change in changes:
        resource, campaign, group = change["resource"], change["campaign"], change.get("ad_group")
        if resource == "campaign":
            lines += _entry(change, 0, f"campaign {campaign}", color)
            shown_campaign, shown_group = campaign, None
            continue
        if campaign != shown_campaign:
            lines.append(f"    campaign {campaign}")
            shown_campaign, shown_group = campaign, None
        if resource == "ad_group":
            lines += _entry(change, 1, f"ad group {group}", color)
            shown_group = group
            continue
        level = 1
        if group:
            level = 2
            if group != shown_group:
                lines.append(f"        ad group {group}")
                shown_group = group
        title = " ".join(part for part in (_TITLE.get(resource, resource), change.get("name") or "") if part)
        if resource == "budget":
            title = _TITLE["budget"]
        lines += _entry(change, level, title, color)
    return lines


def _entry(change: dict[str, Any], level: int, title: str, color: bool) -> list[str]:
    indent = "  " + "    " * level
    action = change["action"]
    line = f"{indent}{_paint(_SYMBOL[action], _COLOR[action], color)} {title}"
    attributes = change.get("attributes") or {}
    if attributes:
        line += "  (" + ", ".join(f"{key} = {_value(value)}" for key, value in attributes.items()) + ")"
    if change.get("note"):
        line += f"  # {change['note']}"
    lines = [line]
    for field, (old, new) in (change.get("diff") or {}).items():
        lines.append(f"{indent}      {field}: {_value(old)} -> {_value(new)}")
    return lines


def _value(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _paint(text: str, code: str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text
