# How Adstack works

Adstack is a reconciler. A configuration describes the campaigns you want; each plan reads the ad account, compares the two, and lists the operations that would make the account match. This page describes what Adstack manages, how plans and applies stay safe, and what it deliberately leaves alone.

## Identity and ownership

Adstack keeps no state file. A campaign's identity is its name:

- Campaigns are named `adstack:<namespace>:<campaign id>`. Ad groups inside them are named `adstack:<ad group id>`.
- Every campaign whose name starts with `adstack:<namespace>:` belongs to the configuration with that namespace. Campaigns outside the namespace are never read or changed, so several configurations (or people) can share an account by using different namespaces.
- The prefix is a naming convention, not a cryptographic label or a lock. Reserve it for Adstack, and don't rename managed campaigns or ad groups by hand.
- Adstack never adopts a campaign with a different name. A campaign in the namespace that isn't a Search campaign with manual CPC bidding, or whose budget is shared with other campaigns, makes planning fail rather than be converted.

## Reconciliation

For each configured campaign, a plan:

- creates the campaign (always `PAUSED`), its dedicated budget, and any missing locations, negative keywords, ad groups, exact-match keywords and responsive search ads, all in one request using temporary resource IDs;
- reconciles the budget amount, campaign status, network settings (Google Search only) and presence-based location targeting, plus each ad group's bid and status and each keyword's status;
- keeps exactly one enabled responsive search ad per managed ad group: when the ad's content changes, it creates a matching ad (or re-enables one that already matches) and pauses the superseded ad.

Things that exist in the account but not in the configuration are never changed silently:

| Found in the account | Default | With `--prune` |
| --- | --- | --- |
| A serving campaign in the namespace that isn't configured | Blocks the plan | Paused |
| An enabled `adstack:` ad group that isn't configured | Blocks the plan | Paused |
| An enabled positive keyword that isn't configured | Blocks the plan | Paused |
| A location or campaign negative keyword that isn't configured | Blocks the plan | Removed (it can be added again later, with a new ID) |
| An active ad group without the `adstack:` prefix, a negative keyword inside an ad group, an excluded location, or an active ad that isn't a responsive search ad | Blocks the plan | Still blocks: Adstack never modifies resources it doesn't own |

Paused campaigns, ad groups and keywords that are no longer configured are left alone. Adstack only reads the settings it manages, so other criteria such as ad schedules or languages are neither checked nor changed. Adstack never deletes campaigns, ad groups, keywords or ads. `adstack destroy` (or `plan --destroy`) pauses every serving campaign in the namespace and changes nothing else; remove campaigns in the Google Ads interface if you need to.

A plan that reports no changes means the **managed subset** matches the configuration. It isn't an audit of the whole account or a guarantee of how ads will deliver.

## Plans

A plan is a JSON document containing:

- the configuration files it was made from, and a hash of the validated configuration;
- the account, the backend (`api` or `sandbox`), and a hash of the account state it was read from;
- the changes for people to review, any blockers, the permissions applying it requires, and the exact API operations.

`adstack plan --out FILE` saves one; plan files are never overwritten. `adstack apply FILE` re-validates the embedded configuration, reads the account again and recomputes the whole plan. Any difference (in the configuration, the account state, the options or the operations) makes it refuse, so a saved plan can only be applied while it still describes exactly what will happen. Without a plan file, `adstack apply` plans, shows the plan, waits for you to type `yes`, and then verifies the plan once more before applying it.

There is no server-side compare-and-swap, so avoid editing the account by hand between the final read and the apply.

## Applying

- **Permissions.** Besides approving the plan, applying it needs `--allow-enable` if a campaign will start serving, `--allow-budget-increase` if a budget or bid on an existing campaign or ad group will increase, and `--allow-live-changes` if anything will change in a campaign that is serving now.
- **Validation first.** The whole request is sent to Google with `validateOnly=true` before it is sent for real.
- **Atomic.** All operations go in one `googleAds:mutate` request with `partialFailure=false`, so they apply completely or not at all. Each resource is changed by at most one operation per request: Google rejects a request that mutates the same resource twice, so changes to several fields of one resource are merged into a single update with a combined field mask.
- **No blind retries.** If the network fails during a mutate, the outcome is unknown. Adstack says so and stops; run `plan` to see what actually happened.
- **Receipts.** Every successful apply writes a receipt to `.adstack/receipts/` with the changes and Google's response. If the receipt can't be written, Adstack reports that the changes were applied anyway and asks you to plan before doing anything else.

## The sandbox

`--sandbox` replaces the Google Ads API with a JSON file in `.adstack/sandbox/`. It applies the same mutate operations Adstack sends to Google and enforces the parts of Google's request contract that Adstack relies on: requests are atomic, temporary IDs must be created before they are referenced, referenced resources must exist, and one request can't mutate a resource twice. It doesn't review ads, check keyword policy, run auctions or bill anyone, and its reports contain zero metrics. Plans made against the sandbox can only be applied to the sandbox.

## What Adstack doesn't manage

Review these separately in Google Ads: they can affect delivery and spend, and Adstack neither sets nor checks them.

- Account settings: billing, auto-apply recommendations, conversion goals and actions, auto-tagging, account-level negative keyword lists and policy settings.
- Campaign settings beyond those above: AI Max and broad match settings, ad schedules, devices, audiences, languages, advanced location options, shared negative keyword lists, assets (sitelinks, callouts and others) and experiments.
- Keyword-level bids, display paths and pinned assets.
- Ad policy review. A valid configuration can still produce a disapproved ad.

Exact match on Google Ads still matches close variants, and `daily_budget` is Google's average daily budget, not a hard cap on daily spend.

## Google Ads API notes

- Adstack uses the Google Ads REST API, version v26.
- Since September 9, 2026, API access belongs to the Google Cloud project that owns the OAuth client; developer tokens are no longer needed, and Adstack doesn't send one.
- Reports use the account's own conversion settings. Google withholds some search terms for privacy.

## Architecture

```
adstack/
  config.py          load YAML files and directories; strict parsing
  engine.py          create, verify and apply plans (shared by the CLI and the MCP server)
  plan.py            plan files, hashing and rendering
  cli.py             the adstack command
  mcp_server.py      the MCP server
  providers/
    base.py          the Provider and Backend interfaces
    google_ads/
      schema.py      configuration validation for Google Ads
      planner.py     pure reconciliation: desired state + snapshot -> plan
      api.py         REST client, account snapshot and reports
      sandbox.py     the offline sandbox account
```

The core workflow knows nothing about Google Ads: providers validate their part of the configuration, read the account, and compute plans. See [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-provider) for adding one.
