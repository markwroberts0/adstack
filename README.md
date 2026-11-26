# Adstack

**Advertising campaigns as code.** Declare campaigns in YAML, review a plan of exactly what will change, then apply it: the workflow Terraform uses for infrastructure, built for ad platforms. An [MCP server](docs/mcp.md) lets AI assistants such as Claude validate, plan and (when you allow it) apply changes too.

[![CI](https://github.com/markwroberts0/adstack/actions/workflows/ci.yml/badge.svg)](https://github.com/markwroberts0/adstack/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

```console
$ adstack plan --out launch.plan.json
Adstack will perform the following actions in Google Ads account 123-456-7890:

  + campaign running-shoes  (status = PAUSED, type = SEARCH, bidding = MANUAL_CPC, networks = Google Search only, geo_targeting = PRESENCE)
      + daily budget  (amount = 25.00 USD)
      + location 2840
      + negative keyword "free"
      + negative keyword "jobs"
      + ad group road  (cpc_bid = 1.10 USD)
          + keyword [road running shoes]
          + keyword [running shoes for men]
          + responsive search ad  (template = running-shoes, headlines = 3, descriptions = 2, final_url = https://www.example.com/running-shoes)

Plan: 9 to add, 0 to change, 0 to remove.

$ adstack apply launch.plan.json
...
Apply complete! Resources: 9 added, 0 changed, 0 removed.
Receipt: .adstack/receipts/20261126T133746Z-44e0f9dbfe49.json
```

This is the plan for the configuration shown [below](#configuration).

## Why Adstack

- **Reviewable.** Campaign changes go through pull requests and a plan you read before anything happens.
- **Safe by default.** New campaigns start paused. Enabling a campaign, raising a budget or bid, and touching a campaign that is serving each need an explicit flag. A saved plan is re-verified against the live account before it is applied, and the platform validates every request before it commits. Adstack never deletes campaigns, ad groups, keywords or ads.
- **Convergent.** The account is made to match the configuration. Changes made by hand show up in the next plan.
- **AI-ready.** The MCP server gives assistants the same validate → plan → apply loop, with applying disabled unless you turn it on.
- **Try it offline.** A local sandbox account runs the whole workflow without an ad account or credentials.

**Status: alpha.** The first provider is **Google Ads** (Search campaigns with manual CPC bidding, exact-match keywords, responsive search ads, negative keywords and location targeting). The plan/apply logic is covered by tests against the sandbox, but the REST integration has not yet been exercised against a live Google Ads account; run it against a [test account](https://developers.google.com/google-ads/api/docs/best-practices/test-accounts) first. More providers are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Install

Adstack needs Python 3.11 or later.

```bash
pip install "adstack[mcp] @ git+https://github.com/markwroberts0/adstack"   # CLI and MCP server
```

Omit `[mcp]` if you only need the CLI. To work on Adstack itself, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start (no ad account needed)

```bash
adstack init my-ads                   # write an annotated starter configuration
adstack validate my-ads               # offline checks
adstack plan my-ads --sandbox         # plan against a local sandbox account
adstack apply my-ads --sandbox        # apply after typing 'yes'
adstack plan my-ads --sandbox         # No changes.
```

The sandbox lives in `.adstack/sandbox/` in the current directory. It applies the same API operations Adstack sends to Google and enforces the same request rules, but it does not review ads or serve them. Two complete examples are in [`examples/`](examples/).

## Configuration

A configuration is one YAML file, or a directory of YAML files that are merged (like a Terraform module):

```yaml
version: 1

provider:
  google_ads:
    namespace: example          # campaigns are named adstack:example:<campaign id>
    currency: USD               # must match the account
    customer_id: 123-456-7890   # optional: pin the account in code review

ad_templates:
  running-shoes:
    final_url: https://www.example.com/running-shoes
    headlines: [Running Shoes For Every Pace, Free Returns On All Shoes, Find Your Perfect Fit]
    descriptions:
      - Road, trail and track shoes from leading brands. Free shipping on orders over $50.
      - Not the right fit? Return any pair within 60 days for a full refund.

campaigns:
  - id: running-shoes
    status: PAUSED
    daily_budget: 25.00
    default_cpc: 1.10
    locations: [2840]                     # United States
    negative_keywords: ['"free"', '"jobs"']
    ad_groups:
      - id: road
        ad_template: running-shoes
        keywords: ['[road running shoes]', '[running shoes for men]']
```

Every option is described in [docs/configuration.md](docs/configuration.md).

## Commands

| Command | What it does |
| --- | --- |
| `adstack init [DIR]` | Write an annotated starter configuration. |
| `adstack validate [PATH]` | Check the configuration offline. |
| `adstack plan [PATH] [--out FILE]` | Read the account and show what would change. Changes nothing. `--prune` also pauses or removes managed resources that are no longer configured; `--validate` asks the platform to validate the changes too; `--detailed-exitcode` exits 2 when there are changes. |
| `adstack apply PLAN_FILE` | Apply a saved plan after verifying the account hasn't changed since it was made. |
| `adstack apply [PATH]` | Plan, then apply after you type `yes` (or `--auto-approve`). |
| `adstack destroy [PATH]` | Pause every campaign Adstack manages. Nothing is deleted. |
| `adstack show PLAN_FILE` | Display a saved plan (`--json` for machines). |
| `adstack state list [PATH]` | List what Adstack manages in the account. |
| `adstack report performance\|search-terms [PATH]` | Clicks, cost and conversions for the last 7, 14 or 30 days. |
| `adstack mcp` | Run the MCP server over stdio. |

`PATH` defaults to the current directory. Add `--sandbox` (or set `ADSTACK_SANDBOX=1`) to use the local sandbox account.

Applying also needs an explicit flag when a plan would:

| Flag | Needed when |
| --- | --- |
| `--allow-enable` | a paused campaign starts serving |
| `--allow-budget-increase` | a budget or bid on an existing campaign or ad group increases |
| `--allow-live-changes` | anything changes in a campaign that is serving now |

Plan files and receipts (written to `.adstack/receipts/` after every apply) are never overwritten. Treat them as private operational data.

## Connect Google Ads

Adstack uses the Google Ads REST API (v26). Since September 9, 2026, API access belongs to the Google Cloud project behind your OAuth credentials; a developer token is no longer needed.

1. In [Google Cloud Console](https://console.cloud.google.com/apis/library/googleads.googleapis.com), select or create a project and enable the Google Ads API.
2. Request an [access level](https://developers.google.com/google-ads/api/docs/api-policy/access-levels) for the project. Test access works only with Google Ads test accounts; start there if you can.
3. Configure the OAuth consent screen and create a Desktop OAuth client. The Google user who authorizes it must have access to the Google Ads account. Obtain a refresh token with the `https://www.googleapis.com/auth/adwords` scope ([Google's OAuth guide](https://developers.google.com/google-ads/api/docs/oauth/overview)). While an external consent screen is in Testing mode, refresh tokens can expire after seven days.
4. Set the credentials in your environment or secret manager. Never commit them. See [`.env.example`](.env.example).

```bash
export GOOGLE_ADS_CUSTOMER_ID=123-456-7890        # or provider.google_ads.customer_id
export GOOGLE_ADS_CLIENT_ID='...'
export GOOGLE_ADS_CLIENT_SECRET='...'
export GOOGLE_ADS_REFRESH_TOKEN='...'
# export GOOGLE_ADS_LOGIN_CUSTOMER_ID=111-222-3333  # only when using a manager account
# export GOOGLE_ADS_ACCESS_TOKEN='...'              # alternative: a short-lived access token
```

Then use the commands without `--sandbox`. Before enabling a campaign, check billing, ad policy status, landing pages and conversion tracking in Google Ads: Adstack doesn't manage those.

## Use with AI assistants (MCP)

```bash
claude mcp add adstack -- adstack mcp --sandbox --root /path/to/your/ads    # Claude Code, sandbox
```

The server's tools are `validate`, `plan`, `show_plan`, `state`, `report` and `example_config`. A configuration can be a path inside `--root` or YAML text. The `apply` tool appears only with `--sandbox` or `--allow-apply`. It accepts only a plan the server created, requires the account ID as confirmation and the same permission flags as the CLI, and re-verifies the plan against the live account. See [docs/mcp.md](docs/mcp.md) for Claude Desktop and other clients.

## Use in CI

Run offline validation (and, if you choose, a plan) on every pull request, and apply reviewed plans deliberately:

```yaml
# .github/workflows/ads.yml in the repository that holds your configuration
on: pull_request
permissions:
  contents: read
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install "adstack @ git+https://github.com/markwroberts0/adstack"
      - run: adstack validate ads/
```

Planning against the real account needs credentials. Don't expose them to workflows that run untrusted pull request code.

## How it works

Adstack keeps no state file. A campaign's identity is its name, `adstack:<namespace>:<campaign id>`, so every campaign in the namespace is managed and every other campaign is ignored. Each plan reads the account, compares it with the configuration, and lists every change, blocker and required permission. Applying sends one atomic request. The details, including what Adstack deliberately doesn't manage, are in [docs/how-it-works.md](docs/how-it-works.md).

## Roadmap

- More providers: Microsoft Advertising, Meta, LinkedIn.
- More Google Ads features: other bidding strategies, phrase and broad keywords in ad groups, display paths, assets, ad schedules.
- Importing existing campaigns into configuration.
- A JSON Schema for editor completion and validation.

Ideas and pull requests are welcome.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues privately, as described in [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE).
