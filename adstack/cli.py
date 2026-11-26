"""The ``adstack`` command line: a Terraform-style workflow for advertising campaigns.

adstack init            create a starter configuration
adstack validate        check the configuration (offline)
adstack plan            show what would change; optionally save the plan
adstack apply           apply a saved plan, or plan and apply after approval
adstack destroy         pause every campaign Adstack manages
adstack show            display a saved plan
adstack state list      list what Adstack manages in the account
adstack report          performance and search-term reports
adstack mcp             run the MCP server for AI assistants
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from . import plan as plans
from .config import load
from .engine import Workspace, apply, connect, create_plan, missing_permissions, target_label, verify
from .errors import AdstackError, ConfigError, PlanError
from .plan import GATES
from .providers import get_provider
from .scaffold import STARTER_CONFIG

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES = 2

DOCS_URL = "https://github.com/markwroberts0/adstack"


class Ui:
    def __init__(self, color: bool):
        self.color = color

    def out(self, text: str = "") -> None:
        print(text)

    def info(self, text: str) -> None:
        """Progress messages go to stderr so that --json output stays parseable."""
        print(text, file=sys.stderr)

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def success(self, text: str) -> None:
        self.out(self.paint(text, "1;32"))

    def error(self, text: str) -> None:
        print(self.paint("Error: ", "1;31") + text, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    no_color = getattr(args, "no_color", False) or "NO_COLOR" in os.environ
    ui = Ui(color=not no_color and sys.stdout.isatty())
    try:
        return args.handler(args, ui)
    except AdstackError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except OSError as exc:
        ui.error(f"{exc.filename or ''}: {exc.strerror or exc}".lstrip(": "))
        return EXIT_ERROR
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130


# -- Commands ---------------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, ui: Ui) -> int:
    directory = Path(args.directory)
    target = directory / "adstack.yaml"
    if target.exists():
        raise ConfigError(f"{target} already exists")
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_CONFIG, encoding="utf-8")
    ui.success(f"Created {target}")
    where = "" if directory == Path(".") else f" {directory}"
    ui.out(
        "\nNext steps:\n"
        f"  1. Edit {target}: set your namespace, currency, ads and keywords.\n"
        f"  2. adstack validate{where}\n"
        f"  3. adstack plan{where} --sandbox     # try it against a local sandbox account\n"
        f"  4. adstack plan{where} --out first.plan.json\n"
        "     adstack apply first.plan.json\n"
        "\nAdd .adstack/ and *.plan.json to .gitignore: they hold local state, plans and receipts."
    )
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, ui: Ui) -> int:
    try:
        config = load(args.path)
    except ConfigError as exc:
        if args.json:
            ui.out(json.dumps({"valid": False, "error": str(exc)}, indent=2))
            return EXIT_ERROR
        raise
    if args.json:
        ui.out(json.dumps({"valid": True, "provider": config.provider, "summary": config.summary()}, indent=2))
    else:
        ui.success("Success! The configuration is valid.")
        ui.out(f"{get_provider(config.provider).title}: {config.summary()}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace, ui: Ui) -> int:
    workspace = _workspace(args)
    config = load(args.path)
    if args.out and Path(args.out).exists():
        raise PlanError(f"{args.out} already exists; plan files are never overwritten, choose a new name")
    backend = connect(config, workspace)
    ui.info(f"Reading the {target_label(config.provider, backend)}...")
    plan = create_plan(config, workspace, prune=args.prune, destroy=args.destroy, backend=backend)
    validated = False
    if args.validate and plan["operations"] and not plan["blockers"]:
        backend.mutate(plan["operations"], validate_only=True)
        validated = True
    ui.out(json.dumps(plan, indent=2, ensure_ascii=False) if args.json else plans.render(plan, color=ui.color))
    if plan["blockers"]:
        raise PlanError("the plan is blocked; resolve the blockers above, or re-plan with --prune where suggested")
    if validated:
        ui.info(f"{get_provider(config.provider).title} validated these changes. Nothing was changed.")
    if args.out:
        plans.save(plan, args.out)
        ui.info(f"\nSaved the plan to {args.out}. To apply exactly this plan, run:\n  adstack apply {args.out}")
    elif plan["operations"] and not args.json:
        ui.info(
            "\nNote: you didn't use --out, so Adstack can't guarantee that exactly these actions will be "
            "taken if you run 'adstack apply' now."
        )
    return EXIT_CHANGES if args.detailed_exitcode and plan["operations"] else EXIT_OK


def cmd_apply(args: argparse.Namespace, ui: Ui) -> int:
    workspace = _workspace(args)
    allow = {gate for gate in GATES if getattr(args, gate, False)}
    target = Path(args.target)
    if plans.is_plan_file(target):
        if args.prune or args.destroy:
            raise PlanError("--prune and --destroy can't be used with a saved plan; they were set when it was created")
        saved = plans.load(target)
        if args.sandbox and saved.get("backend") != "sandbox":
            raise PlanError("this plan was made against the real platform, not the sandbox")
        ui.info(f"Verifying {target} against the current state of the {saved.get('target', 'account')}...")
        fresh, backend = verify(saved, workspace)
        ui.out(plans.render(fresh, color=ui.color))
        verified: Any = (fresh, backend)
        plan = fresh
    else:
        config = load(target)
        backend = connect(config, workspace)
        ui.info(f"Reading the {target_label(config.provider, backend)}...")
        plan = create_plan(config, workspace, prune=args.prune, destroy=args.destroy, backend=backend)
        ui.out(plans.render(plan, color=ui.color))
        verified = None  # re-verified after approval, in case the account changed meanwhile
        if plan["blockers"]:
            raise PlanError("the plan is blocked; resolve the blockers above, or re-plan with --prune where suggested")
    if not plan["operations"]:
        return EXIT_OK
    missing = missing_permissions(plan, allow)
    if missing:
        flags = " ".join(f"--{gate.replace('_', '-')}" for gate in missing)
        raise PlanError(f"this plan needs explicit permission; review it, then add {flags}")
    if verified is None and not args.auto_approve:
        _confirm(ui, plan)
    result = apply(plan, workspace, allow=allow, verified=verified)
    if result is None:
        return EXIT_OK
    receipt, path = result
    total = receipt["summary"]
    ui.success(
        f"\nApply complete! Resources: {total['add']} added, {total['change']} changed, {total['remove']} removed."
    )
    ui.out(f"Receipt: {_relative(path)}")
    return EXIT_OK


def cmd_destroy(args: argparse.Namespace, ui: Ui) -> int:
    args.target, args.prune, args.destroy = args.path, False, True
    args.allow_enable = args.allow_budget_increase = False
    return cmd_apply(args, ui)


def cmd_show(args: argparse.Namespace, ui: Ui) -> int:
    plan = plans.load(args.plan)
    if args.json:
        ui.out(json.dumps(plan, indent=2, ensure_ascii=False))
        return EXIT_OK
    ui.out(plans.render(plan, color=ui.color))
    ui.out(
        f"\nCreated {plan.get('created_at')} by adstack {plan.get('adstack_version')} from "
        f"{', '.join(plan['config']['files'])} ({plan['config'].get('summary')})."
    )
    return EXIT_OK


def cmd_state_list(args: argparse.Namespace, ui: Ui) -> int:
    workspace = _workspace(args)
    config = load(args.path)
    provider = get_provider(config.provider)
    backend = connect(config, workspace)
    target = target_label(config.provider, backend)
    records = provider.inventory(config.desired, backend.read())
    if args.json:
        ui.out(json.dumps({"target": target, "resources": records}, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not records:
        ui.out(f"Adstack manages nothing in the {target} yet.")
        return EXIT_OK
    ui.out(f"Managed resources in the {target}:\n")
    for line in render_inventory(records):
        ui.out(line)
    return EXIT_OK


def cmd_report(args: argparse.Namespace, ui: Ui) -> int:
    workspace = _workspace(args)
    config = load(args.path)
    backend = connect(config, workspace)
    rows = backend.report(args.kind.replace("-", "_"), args.days, all_campaigns=args.all_campaigns)
    if args.json:
        ui.out(json.dumps(rows, indent=2, ensure_ascii=False))
        return EXIT_OK
    currency = config.desired.get("currency", "")
    title = args.kind.replace("-", " ").capitalize()
    ui.out(f"{title}, last {args.days} days ({target_label(config.provider, backend)}, cost in {currency}):\n")
    if not rows:
        ui.out("No data.")
        return EXIT_OK
    for line in render_table(rows):
        ui.out(line)
    return EXIT_OK


def cmd_mcp(args: argparse.Namespace, ui: Ui) -> int:
    try:
        from .mcp_server import serve
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "mcp":
            raise AdstackError(
                f"the MCP server needs the 'mcp' package: install Adstack with the [mcp] extra (see {DOCS_URL}#install)"
            ) from None
        raise
    allow_apply = args.allow_apply or _env_flag("ADSTACK_MCP_ALLOW_APPLY")
    serve(root=Path(args.root).resolve(), sandbox=args.sandbox, allow_apply=allow_apply)
    return EXIT_OK


def cmd_version(args: argparse.Namespace, ui: Ui) -> int:
    ui.out(f"adstack {__version__}")
    return EXIT_OK


# -- Helpers ---------------------------------------------------------------------------------------


def _relative(path: Path) -> Path:
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _workspace(args: argparse.Namespace) -> Workspace:
    return Workspace(root=Path.cwd(), sandbox=args.sandbox, env=os.environ)


def _confirm(ui: Ui, plan: dict[str, Any]) -> None:
    if not sys.stdin.isatty():
        raise PlanError(
            "apply needs approval: run it in an interactive terminal, apply a saved plan file, or pass --auto-approve"
        )
    ui.out(
        f"\nDo you want to perform these actions in the {plan['target']}?\n  Only 'yes' will be accepted to approve.\n"
    )
    if input("  Enter a value: ").strip() != "yes":
        raise PlanError("Apply cancelled.")


def render_inventory(records: list[dict[str, Any]]) -> list[str]:
    lines = []
    for record in records:
        kind = record["resource"]
        if kind == "campaign":
            depth, title = 0, f"campaign {record['campaign']}"
        elif kind == "ad_group":
            depth, title = 1, f"ad group {record['ad_group']}"
        elif kind == "ad":
            depth, title = 2, record["name"]
        else:
            depth, title = (2 if record.get("ad_group") else 1), f"{kind.replace('_', ' ')} {record['name']}"
        details = f"  {record['details']}" if record.get("details") else ""
        lines.append(f"{'  ' * depth}{title}  [{record.get('status')}]{details}")
    return lines


def render_table(rows: list[dict[str, Any]]) -> list[str]:
    columns = list(rows[0])
    cells = [[str(row.get(column, "")) for column in columns] for row in rows]
    widths = [max(len(column), *(len(row[i]) for row in cells)) for i, column in enumerate(columns)]
    numeric = [all(_is_number(row[i]) for row in cells) for i in range(len(columns))]

    def fmt(values: list[str]) -> str:
        cells = zip(values, widths, numeric, strict=True)
        return "  ".join(v.rjust(w) if n else v.ljust(w) for v, w, n in cells).rstrip()

    return [fmt(columns), fmt(["-" * w for w in widths]), *(fmt(row) for row in cells)]


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


# -- Parser ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adstack",
        description="Advertising campaigns as code: declare campaigns in YAML, review a plan, apply it.",
        epilog=f"Run 'adstack <command> --help' for a command's options. Documentation: {DOCS_URL}",
    )
    parser.add_argument("--version", action="version", version=f"adstack {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="disable colored output (or set NO_COLOR)")
    account = argparse.ArgumentParser(add_help=False)
    account.add_argument(
        "--sandbox",
        action="store_true",
        default=_env_flag("ADSTACK_SANDBOX"),
        help="use a local sandbox account (.adstack/sandbox) instead of the real ad platform "
        "(or set ADSTACK_SANDBOX=1)",
    )
    path_help = "configuration file or directory (default: the current directory)"

    p = commands.add_parser(
        "init",
        parents=[common],
        help="create a starter configuration",
        description="Write an annotated starter configuration to DIRECTORY/adstack.yaml.",
    )
    p.add_argument("directory", nargs="?", default=".", help="where to create adstack.yaml (default: .)")
    p.set_defaults(handler=cmd_init)

    p = commands.add_parser(
        "validate",
        parents=[common],
        help="check the configuration (offline)",
        description="Validate the configuration without contacting the ad platform.",
    )
    p.add_argument("path", nargs="?", default=".", help=path_help)
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(handler=cmd_validate)

    p = commands.add_parser(
        "plan",
        parents=[common, account],
        help="show what would change",
        description="Read the account, compare it with the configuration and show the changes. Changes nothing.",
    )
    p.add_argument("path", nargs="?", default=".", help=path_help)
    p.add_argument("-o", "--out", metavar="FILE", help="save the plan to FILE for 'adstack apply FILE'")
    p.add_argument(
        "--prune", action="store_true", help="pause or remove managed resources that are no longer in the configuration"
    )
    p.add_argument("--destroy", action="store_true", help="plan to pause every managed campaign")
    p.add_argument(
        "--validate",
        action="store_true",
        help="also ask the platform to validate the changes (validate-only; changes nothing)",
    )
    p.add_argument(
        "--detailed-exitcode",
        action="store_true",
        help="exit 0 when there are no changes, 2 when there are changes, 1 on errors",
    )
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.set_defaults(handler=cmd_plan)

    gates = argparse.ArgumentParser(add_help=False)
    gates.add_argument("--auto-approve", action="store_true", help="skip the interactive approval prompt")
    gates.add_argument("--allow-live-changes", action="store_true", help="allow changes to campaigns that are serving")

    p = commands.add_parser(
        "apply",
        parents=[common, account, gates],
        help="apply a saved plan, or plan and apply after approval",
        description="Apply a saved plan file (after verifying it is still current), or plan from a configuration "
        "and apply after interactive approval.",
    )
    p.add_argument("target", nargs="?", default=".", help="a saved plan file, or a configuration file or directory")
    p.add_argument(
        "--prune",
        action="store_true",
        help="when planning: pause or remove resources that are not in the configuration",
    )
    p.add_argument("--destroy", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--allow-enable", action="store_true", help="allow a paused campaign to start serving")
    p.add_argument("--allow-budget-increase", action="store_true", help="allow budget or bid increases")
    p.set_defaults(handler=cmd_apply)

    p = commands.add_parser(
        "destroy",
        parents=[common, account, gates],
        help="pause every campaign Adstack manages",
        description="Plan to pause every campaign in the namespace, then apply after approval. Adstack never "
        "deletes campaigns; remove them in the ad platform's interface if you need to.",
    )
    p.add_argument("path", nargs="?", default=".", help=path_help)
    p.set_defaults(handler=cmd_destroy)

    p = commands.add_parser("show", parents=[common], help="display a saved plan")
    p.add_argument("plan", help="plan file created with 'adstack plan --out'")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.set_defaults(handler=cmd_show)

    p = commands.add_parser("state", help="inspect what Adstack manages in the account")
    state = p.add_subparsers(dest="state_command", required=True, metavar="<subcommand>")
    p = state.add_parser("list", parents=[common, account], help="list managed resources in the account")
    p.add_argument("path", nargs="?", default=".", help=path_help)
    p.add_argument("--json", action="store_true", help="print the resources as JSON")
    p.set_defaults(handler=cmd_state_list)

    p = commands.add_parser("report", parents=[common, account], help="performance and search-term reports")
    p.add_argument("kind", choices=["performance", "search-terms"])
    p.add_argument("path", nargs="?", default=".", help=path_help)
    p.add_argument("--days", type=int, choices=[7, 14, 30], default=7, help="reporting window (default: 7)")
    p.add_argument(
        "--all-campaigns", action="store_true", help="include campaigns outside the configuration's namespace"
    )
    p.add_argument("--json", action="store_true", help="print rows as JSON")
    p.set_defaults(handler=cmd_report)

    p = commands.add_parser(
        "mcp",
        parents=[account],
        help="run the MCP server (stdio) for AI assistants",
        description="Serve Adstack's tools over the Model Context Protocol (stdio). Applying changes is disabled "
        "unless --allow-apply is set or the server uses the sandbox.",
    )
    p.add_argument("--root", default=".", help="directory the tools may read configurations from (default: .)")
    p.add_argument(
        "--allow-apply",
        action="store_true",
        help="expose the apply tool for the real ad platform; or set ADSTACK_MCP_ALLOW_APPLY=1",
    )
    p.set_defaults(handler=cmd_mcp)

    p = commands.add_parser("version", help="print the Adstack version")
    p.set_defaults(handler=cmd_version)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
