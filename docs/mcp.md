# MCP server

`adstack mcp` serves Adstack over the [Model Context Protocol](https://modelcontextprotocol.io) (stdio), so an AI assistant can write a configuration, validate it, show you a plan, and, if you allow it, apply that plan after you approve it.

Install it with the `mcp` extra: `pip install "adstack[mcp]"`.

## Modes

| Command | Account | `apply` tool |
| --- | --- | --- |
| `adstack mcp --sandbox` | Local sandbox in `<root>/.adstack/sandbox/` | Available: nothing reaches a real ad platform. |
| `adstack mcp` | The real account, from the configuration and environment | Not available. The assistant can validate, plan, list state and read reports. |
| `adstack mcp --allow-apply` | The real account | Available, with the safeguards [below](#safeguards). `ADSTACK_MCP_ALLOW_APPLY=1` does the same. |

`--root DIR` (default: the directory the client starts the server in) is the only directory the tools can read configurations from. Plans the server creates are saved in `<root>/.adstack/plans/`, and receipts in `<root>/.adstack/receipts/`.

Credentials come from the server's environment, as for the CLI: `GOOGLE_ADS_CUSTOMER_ID` (unless the configuration sets `customer_id`), then `GOOGLE_ADS_ACCESS_TOKEN`, or `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET` and `GOOGLE_ADS_REFRESH_TOKEN`. The sandbox needs none.

## Connect a client

**Claude Code**

```bash
claude mcp add adstack -- adstack mcp --sandbox --root /path/to/your/ads
```

Drop `--sandbox` to plan against the real account, and add `--allow-apply` only if you want Claude to be able to apply approved plans.

**Claude Desktop** and other clients that read an `mcpServers` configuration file:

```json
{
  "mcpServers": {
    "adstack": {
      "command": "/absolute/path/to/adstack",
      "args": ["mcp", "--root", "/path/to/your/ads"],
      "env": {
        "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
        "GOOGLE_ADS_ACCESS_TOKEN": "..."
      }
    }
  }
}
```

Desktop apps often don't inherit your shell's `PATH`, so use the absolute path that `which adstack` prints. Values in `env` are stored in plain text: prefer a short-lived access token, or a small wrapper script that reads credentials from your secret manager, over a long-lived refresh token.

## Tools

Tools that take a configuration accept either `path` (a file or directory relative to the root; default: the root) or `config` (YAML text, for configurations that exist only in the conversation).

| Tool | Changes anything? | What it does |
| --- | --- | --- |
| `validate` | No | Checks a configuration offline. Returns `valid` and a summary, or the exact problem in `error`. |
| `plan` | No (writes a local plan file) | Reads the account and computes the changes. Returns `plan_text` (the same text as `adstack plan`), structured `changes`, `blockers`, the permissions it `requires`, the `account`, a `plan_id` when it can be applied, and a `next_step` hint. `prune` and `destroy` work as in the CLI. |
| `show_plan` | No | Shows a plan the server created earlier. |
| `apply` | **Yes** | Applies a plan by `plan_id`. Present only with `--sandbox` or `--allow-apply`. |
| `state` | No | Lists the campaigns, ad groups, keywords and ads Adstack manages, with their statuses. |
| `report` | No | Performance or search terms for the last 7, 14 or 30 days. Cost is in account currency. |
| `example_config` | No | An annotated example configuration that documents every option. |

The same example configuration is also available as the resource `adstack://docs/example-config`. The server's instructions describe the validate → plan → approve → apply workflow to the assistant.

## Safeguards

Applying changes to a real ad account spends money, so the server never relies on the assistant's judgment alone:

1. **Off unless enabled.** Without `--sandbox` or `--allow-apply`, the server has no `apply` tool, so nothing it does can change the account.
2. **Only reviewed plans.** `apply` takes a `plan_id` from the `plan` tool. The assistant can't submit arbitrary API operations, and plan IDs are validated before any file is read.
3. **Account confirmation.** `apply` requires `confirm_account`, which must equal the plan's account. It appears in the tool call your client shows you.
4. **The same permissions as the CLI.** Plans that enable a campaign, raise a budget or bid, or change a serving campaign need `allow_enable`, `allow_budget_increase` or `allow_live_changes`. The server's instructions tell the assistant to set these only when you approved that effect.
5. **Re-verified at apply time.** The plan is recomputed from its embedded configuration and the account's current state. If anything changed since you reviewed it, `apply` refuses and the assistant has to plan again.
6. **Hints for your client.** `apply` is annotated as destructive and every other tool as non-destructive, so clients can ask you before every `apply` while running read-only tools freely. Configure your client to always ask before `apply`.
7. **Contained file access.** Paths outside `--root` are rejected.

Every apply writes a receipt to `<root>/.adstack/receipts/`, so you can see what changed and when.
