# Changelog

## 0.2.0 (unreleased)

Adstack is now a general-purpose, open-source tool with a Terraform-style workflow and an MCP server.

### Added

- `adstack` command with `init`, `validate`, `plan`, `apply`, `destroy`, `show`, `state list`, `report`, `mcp` and `version`.
- `plan --out` saves a self-contained plan. It embeds its configuration, so `apply PLAN_FILE` needs no configuration files, and it is recomputed and refused if anything changed. `plan --detailed-exitcode` exits 2 when there are changes; `plan --json` and `show --json` print machine-readable plans.
- `apply` without a plan file plans and then asks for approval (`yes`), or proceeds with `--auto-approve`.
- `destroy` pauses every campaign in the namespace. Adstack still never deletes campaigns.
- Several campaigns per configuration, and configurations split across the YAML files in a directory.
- Campaigns that are in the namespace but no longer configured block the plan until it is pruned (`--prune` pauses them).
- A local sandbox account (`--sandbox`) for trying configurations, plans and the MCP server without an ad account.
- MCP server (`adstack mcp`): `validate`, `plan`, `show_plan`, `state`, `report`, `example_config`, and `apply` when the server uses the sandbox or runs with `--allow-apply`.
- Receipts are written automatically to `.adstack/receipts/` after every apply.
- A provider interface, so ad platforms other than Google Ads can be added.
- Inline ads in ad groups (`ad:`) as an alternative to `ad_template`.
- `provider.google_ads.customer_id` and `login_customer_id` can pin the account in the configuration.
- Configurable `limits` for budgets and bids.
- Terraform-style plan output, grouped by campaign and ad group, with color when the output is a terminal.

### Changed

- The project is no longer tied to one advertiser: the advertiser-specific example was replaced by fictional ones.
- The command is `adstack` (was `ads`), and the package is `adstack` (was `adstack-cli`).
- New configuration layout: `namespace` and `currency` moved into `provider.google_ads`, and `campaign` became the list `campaigns`. See below.
- Managed names start with `adstack:` (was `aac:`).
- Environment variables are named like Google's client libraries: `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`, `GOOGLE_ADS_ACCESS_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET` and `GOOGLE_ADS_REFRESH_TOKEN` (were `ADS_*`).
- The fixed caps (500 daily budget, 20 CPC) became configurable limits with defaults of 1000 and 50.
- Released under the Apache License 2.0.

### Fixed

- Plans that changed several fields of one resource (for example an ad group's bid and status) sent one operation per field, which Google rejects (`ID_EXISTS_IN_MULTIPLE_MUTATES`). They are now merged into one operation.
- Review and performance metadata on ads no longer changes the account snapshot, so it can't make a saved plan look stale.

### Migrating from 0.1

```yaml
# 0.1                                  # 0.2
version: 1                             version: 1
namespace: shop                        provider:
currency: USD                            google_ads:
campaign:                                  namespace: shop
  id: search                               currency: USD
  ...                                  campaigns:
ad_templates: {...}                      - id: search
                                           ...
                                       ad_templates: {...}
```

Rename the `ADS_*` environment variables to `GOOGLE_ADS_*`. Version 0.1 was never run against a live account, so there are no `aac:` campaigns to rename. If you created some, rename them to `adstack:<namespace>:<id>` (and their ad groups to `adstack:<id>`) before planning.

## 0.1.0

Initial local version: one Google Search campaign per YAML file, reviewed plans, applies validated by Google, and safety flags for enabling campaigns, increasing budgets and bids, and changing live campaigns.
