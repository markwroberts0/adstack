# Contributing to Adstack

Thanks for helping. Adstack changes real advertising accounts that spend real money, so changes are judged first on whether they keep the workflow safe and predictable, and then on features.

## Development setup

```bash
git clone https://github.com/markwroberts0/adstack
cd adstack
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest -q                 # the whole suite runs offline in a few seconds
ruff check . && ruff format --check .
```

Try your change end to end against the sandbox:

```bash
adstack plan examples/quickstart --sandbox
adstack apply examples/quickstart --sandbox --auto-approve
adstack mcp --sandbox     # for MCP changes; connect a client as in docs/mcp.md
```

## Ground rules

- **No network in tests.** Tests use the sandbox backend, fake HTTP sessions and canned API rows. CI never has ad platform credentials, and pull requests must not require them.
- **Planning is pure.** A provider's `plan` function takes the desired state and a snapshot and returns the same plan every time, with no I/O. Saved plans are verified by recomputing them, so any non-determinism (timestamps, set ordering, random IDs) breaks applies.
- **Fail closed.** When the account contains something Adstack doesn't understand or doesn't own, report a blocker or raise an error. Never guess, adopt or delete.
- **Explicit permissions stay explicit.** Anything that can start or increase spend, or touch a serving campaign, must be reflected in the plan's `requires`.
- **Keep the sandbox honest.** If you depend on a platform rule (ordering, uniqueness, atomicity), make the sandbox enforce it too, so tests catch violations.
- **Document behavior changes.** Update `docs/` and `CHANGELOG.md` with user-visible changes.

## Project layout

See the [architecture overview](docs/how-it-works.md#architecture). In short: `config.py`, `engine.py`, `plan.py`, `cli.py` and `mcp_server.py` are provider-independent; everything specific to an ad platform lives in `adstack/providers/<name>/`.

## Adding a provider

A provider implements the `Provider` protocol in [`adstack/providers/base.py`](adstack/providers/base.py):

| Method | Responsibility |
| --- | --- |
| `load(settings, document)` | Validate the provider block and the rest of the configuration (strictly) and return a JSON-serializable desired state. |
| `summary(desired)` | One line such as `2 campaigns, 5 ad groups, 23 keywords`. |
| `connect(desired, sandbox_dir=..., env=...)` | Return a `Backend` for the real account (credentials from `env`) or, when `sandbox_dir` is set, an offline sandbox. |
| `plan(desired, state, account, prune=..., destroy=...)` | Pure reconciliation. Return `changes` (records with `action`, `resource`, `campaign`, `ad_group`, `name` and optional `attributes`, `diff`, `note`), `blockers`, `requires` (the three permission flags) and `operations`. |
| `inventory(desired, state)` | Flatten a snapshot into records for `adstack state list`. |
| `format_account(account)` | Display form of an account ID. |

A `Backend` has `account`, `sandbox`, `read()`, `mutate(operations, validate_only=...)` and `report(kind, days, all_campaigns=...)`. Mutations must be atomic, or the provider must explain clearly why they can't be. Register the provider in `adstack/providers/__init__.py`.

Ship a sandbox backend with the provider, plus tests covering at least: an initial plan, a no-op re-plan after applying it, drift correction, prune, destroy, the permission flags, and the platform rules the planner depends on.

## Pull requests

- Keep each pull request focused, and describe the user-visible behavior change.
- Add or update tests; `pytest`, `ruff check` and `ruff format --check` must pass.
- For changes to API payloads or queries, link the platform documentation you relied on, and say whether you tested against a real (test) account.

## Reporting bugs

Open an issue with the Adstack version, the command you ran, the configuration (redact anything private) and the output. Never paste credentials, plan files or receipts from a real account into an issue: they contain account details. Report security problems privately, as described in [SECURITY.md](SECURITY.md).
