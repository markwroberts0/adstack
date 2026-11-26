# Adstack — ads as code

A small, declarative CLI for Google Search campaigns. Define campaigns in YAML, review the proposed changes as a plan, and apply them with explicit authorization. The initial example is the Klora competitor campaign: 17 ad groups, 50 exact-match keywords, eight campaign-level negative keywords, three reusable responsive search ad templates, and one dedicated daily budget.

## Quick start (offline)

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
ads validate examples/klora.yaml
ads preview examples/klora.yaml
pytest -q
```

`validate` and `preview` do not make network requests. The preview assumes the campaign doesn't exist and cannot be applied. No Google account or GitHub connection is needed for these commands.

## Connect Google Ads

Adstack targets the Google Ads REST API v26. Since September 2026, API access belongs to the Google Cloud project associated with your OAuth credentials, rather than a legacy developer token.

1. In [Google Cloud Console](https://console.cloud.google.com/apis/library/googleads.googleapis.com), select or create a project and enable the Google Ads API.
2. On the Google Ads API Overview page, request Explorer access for production accounts. Test access only works against Google Ads test accounts. Start with a test account if possible.
3. Configure the OAuth consent screen and create a Desktop OAuth client. The authorized Google user must have access to the Google Ads customer account. Obtain a refresh token with the `https://www.googleapis.com/auth/adwords` scope, following [Google's OAuth guide](https://developers.google.com/google-ads/api/docs/oauth/overview). If an external OAuth consent screen remains in Testing mode, refresh tokens may expire after seven days.
4. Set these environment variables in your shell or local secret manager (never commit them):

```bash
export ADS_CUSTOMER_ID=1234567890  # replace with your actual customer ID
export ADS_CLIENT_ID='...'
export ADS_CLIENT_SECRET='...'
export ADS_REFRESH_TOKEN='...'
# Only if accessing the advertiser through a manager account:
# export ADS_LOGIN_CUSTOMER_ID=9876543210
```

Alternatively, set `ADS_ACCESS_TOKEN` to a short-lived OAuth access token with the Ads scope, instead of the client credentials. `--customer-id` can override `ADS_CUSTOMER_ID` for a command. `.env.example` documents variables but is not loaded automatically. Treat plan and receipt files as private operational data. Do not paste OAuth secrets into a chat or commit them into Git.

## Manage a campaign

The example uses a proposed USD 10 average daily budget, USD 0.75 manual CPC at the ad-group level, US geographic presence targeting (GeoTargetConstant 2840), Google Search only, no search partners or display expansion, and a paused campaign. Review the settings and ad claims before uploading. The daily budget is Google's average daily budget, not a hard per-day spending cap. The CPC is a starting assumption, not measured auction data. A new campaign cannot be created enabled.

```bash
# 1. Read live configuration and write the proposed mutations.
ads plan examples/klora.yaml --out first.plan.json

# 2. Inspect first.plan.json, then validate the identical plan against Google.
ads apply examples/klora.yaml --plan first.plan.json

# 3. Explicitly commit the reviewed plan (still PAUSED).
ads apply examples/klora.yaml --plan first.plan.json --commit \
  --confirm-account "$ADS_CUSTOMER_ID" --receipt first.receipt.json

# 4. Re-plan; this should be a no-op after a successful first creation.
ads plan examples/klora.yaml --out after.plan.json
```

You can also ask Google to validate the operations while planning using `--validate-api`. Google validation uses `validateOnly=true`, which doesn't commit mutations.

To activate a campaign, explicitly change `status` to `ENABLED` in the YAML, create a **new** plan, review it, and apply with `--commit --confirm-account ... --receipt ... --allow-enable`. Check billing, ad policy status, landing pages and conversion tracking before enabling. The CLI does not provision tracking or verify that the desired conversion actions exist. Existing live campaign mutations also require `--allow-live-changes`; budget or bid increases on existing resources require `--allow-budget-increase`. These flags are in addition to `--commit`. To pause a currently live campaign, set `status: PAUSED`, review, and use the live-changes flag.

```bash
ads performance --days 30
ads search-terms --days 7
```

Reports return Google Ads JSON for the selected account (all campaigns, not just Adstack campaigns). Cost is reported in micros of the account currency; divide by 1,000,000. Google withholds some search terms for privacy. The report uses the account's configured conversions, not a custom Klora conversion definition.

## Reconciliation contract

- The campaign's stable identity is `aac:<namespace>:<id>`; group identity is `aac:<id>`. Reserve those names exclusively for this tool. Do not rename them in Google Ads. The prefix is an ownership convention, not a cryptographically verified label or state lock. There is no automatic adoption of differently named campaigns. A matching name is treated as managed. A conflicting type or shared budget fails closed.
- Missing groups, exact-match keywords, locations and negatives are created. Existing budget, campaign status, networks, presence targeting, group CPC and statuses are reconciled. Copy changes create or reuse an equivalent responsive search ad and pause superseded RSAs. One active RSA per managed ad group is the contract; don't manually create additional RSA variants in those groups unless you're prepared for them to be paused.
- Extra active positive keywords and `aac:` ad groups block application by default. `plan --prune` explicitly requests pausing those extras and removing undesired managed locations or campaign negative keyword criteria. Removed location/negative criteria can be recreated later but get new IDs. No campaign deletion, group deletion, or ad deletion is implemented. Non-managed active groups, negative group keywords, negative geographic exclusions and non-RSA active ads cause blockers where observed.
- `apply` recomputes the plan from the current account and YAML. Any difference from the reviewed file is rejected, including account, desired configuration, scoped resource state, and exact operation list. There is no server-side compare-and-swap; avoid concurrent account edits between the final read and mutation. New resources are created atomically in one `googleAds:mutate` request with negative temporary resource IDs. All requests use `partialFailure=false`.
- The CLI validates the entire proposed request through Google before committing. It never blindly retries a mutation after a network failure; re-plan to learn what actually happened. Successful commits write a local receipt. Plan/receipt files are never overwritten.
- Exact match does not mean literal equality on Google Ads: close variants are eligible. This tool does not manage campaign-wide broad-match settings, AI Max, account-level auto-apply, shared negative lists, schedules, keyword-specific bid overrides, asset extensions, advanced targeting, conversion goals, auto-tagging, or all account/campaign-level policy settings. Review these separately. A successful plan is a match for the **managed subset**, not a comprehensive account audit or a guarantee of zero delivery drift.

## Git workflow

Commit the YAML, implementation and tests. Keep OAuth secrets and operational plans/receipts out of source control. Have a PR change the desired state, run offline validation and tests in CI, review a live plan separately, then execute the reviewed plan manually with account confirmation. The included GitHub Actions workflow runs offline tests only; it does not authenticate or deploy campaigns. No credentials should be exposed to untrusted PR code.

## Status and limitations

Version 0.1 is a local MVP. The example, reconciliation, mutation ordering and safety gates are covered by offline tests. The REST payloads and GAQL queries were designed against the v26 documentation, but **have not been exercised against a Google Ads account**. Before production use, run a test-account smoke test (create paused, read back, verify a no-op plan, modify one keyword/ad, verify policy responses), then validate the production plan. No API request was made during development. The initial budget, bids, location and landing page are proposed settings, not approved by the account owner. Multi-campaign bundles, import/adoption, keyword research, conversion provisioning, a hosted MCP server and automated CI deployment are future work.

References: [API access](https://developers.google.com/google-ads/api/docs/api-policy/access-levels), [developer token sunset](https://developers.google.com/google-ads/api/docs/api-policy/developer-token), [Search campaign creation](https://developers.google.com/google-ads/api/docs/campaigns/search-campaigns/getting-started), [atomic mutations](https://developers.google.com/google-ads/api/docs/mutating/overview), [REST authentication](https://developers.google.com/google-ads/api/rest/auth).
