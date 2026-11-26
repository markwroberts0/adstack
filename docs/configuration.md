# Configuration reference

An Adstack configuration describes the campaigns you want. Adstack compares it with the ad account and plans the changes that make the account match.

- [Files](#files)
- [Provider: `google_ads`](#provider-google_ads)
- [Ad templates](#ad_templates)
- [Campaigns](#campaigns)
- [Ad groups](#ad-groups)
- [Keywords](#keywords)
- [Responsive search ads](#responsive-search-ads)
- [Names and ownership](#names-and-ownership)

`adstack init` writes an annotated starter configuration, and [`examples/`](../examples/) has two complete ones.

## Files

A configuration is either one YAML file or a directory. In a directory, Adstack reads every `*.yaml` and `*.yml` file directly inside it (not subdirectories, and not files whose names start with a dot), in name order, and merges them:

- Lists are concatenated, so each file can add `campaigns`.
- Mappings are combined, so `ad_templates` can be spread across files. Defining the same template or provider twice is an error that names both files.
- Every file must start with `version: 1`, the configuration format version.

Keep configuration files in their own directory: any other YAML file there is read too and rejected as invalid. Validation is strict: unknown keys, duplicate keys, wrong types and out-of-range values are all errors, and the message names the exact location, for example `campaigns.brand.ad_groups.coffee.keywords: '"coffee"': ad group keywords must use exact match, written [keyword]`.

## Provider: `google_ads`

```yaml
provider:
  google_ads:
    namespace: example            # required
    currency: USD                 # required
    customer_id: 123-456-7890     # optional
    login_customer_id: 111-222-3333   # optional
    limits:                       # optional
      daily_budget: 1000
      cpc: 50
```

| Key | Description |
| --- | --- |
| `namespace` | Required. 1-60 lowercase letters, digits and hyphens. Adstack owns every campaign named `adstack:<namespace>:…` in the account; see [Names and ownership](#names-and-ownership). Use different namespaces for configurations that share an account. |
| `currency` | Required. The account's ISO 4217 currency code, such as `USD`. Every amount in the configuration is in this currency. Plans fail if the account uses a different currency. |
| `customer_id` | The 10-digit Google Ads account ID, with or without dashes. When set, the account is part of the reviewed configuration, and `GOOGLE_ADS_CUSTOMER_ID` must be unset or agree. When omitted, `GOOGLE_ADS_CUSTOMER_ID` is used. |
| `login_customer_id` | The manager account to authenticate through, if any. Can also come from `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. |
| `limits` | Upper bounds that catch typos such as an extra zero. `daily_budget` caps each campaign's `daily_budget` (default 1000) and `cpc` caps `default_cpc` and every ad group's `cpc` (default 50). Raise them if your real amounts are larger, for example in a currency with large units. |

Amounts are decimal numbers with at most two decimal places, from 0.01 up to the limit. Credentials are never part of the configuration; see [Connect Google Ads](../README.md#connect-google-ads).

## `ad_templates`

Optional. Named responsive search ads that ad groups can share:

```yaml
ad_templates:
  brand:                          # template name: lowercase letters, digits and hyphens
    final_url: https://www.example.com/
    headlines: [Example Coffee Co. Official, Small-Batch Roasted Coffee, Free Shipping Over $30]
    descriptions:
      - Fresh coffee roasted to order and shipped within 48 hours.
      - Single origins, blends and decaf. Satisfaction guaranteed.
```

The fields are described under [Responsive search ads](#responsive-search-ads).

## `campaigns`

Required. A list of Google Search campaigns. It may be empty, in which case every serving campaign in the namespace blocks the plan as no longer configured (see [Names and ownership](#names-and-ownership)).

```yaml
campaigns:
  - id: brand
    status: PAUSED
    daily_budget: 15.00
    default_cpc: 0.60
    locations: [2840, 2124]
    negative_keywords: ['"jobs"', '[free coffee]', 'careers']
    ad_groups: [...]
```

| Key | Description |
| --- | --- |
| `id` | Required. 1-60 lowercase letters, digits and hyphens, unique in the configuration. The campaign's name in Google Ads is `adstack:<namespace>:<id>`. Changing the id creates a new campaign; the old one is then no longer configured, which blocks the plan while it is serving. |
| `status` | Required. `PAUSED` or `ENABLED`. New campaigns must be created `PAUSED`; enable them in a later plan, which then needs `--allow-enable`. |
| `daily_budget` | Required. The campaign's dedicated average daily budget. Google can spend more than this on some days; it is not a hard daily cap. |
| `default_cpc` | Required. The manual CPC bid for ad groups that don't set `cpc`. |
| `locations` | Required. 1-100 [geo target constant](https://developers.google.com/google-ads/api/data/geotargets) IDs, such as `2840` for the United States. Targeting uses presence: people in or regularly in these locations. |
| `negative_keywords` | Optional. Up to 500 campaign-level negative keywords in any [match type](#keywords). |
| `ad_groups` | Required. 1-100 [ad groups](#ad-groups). |

Every campaign is a Search campaign on Google Search only (no search partners or Display expansion), with presence-based location targeting, manual CPC bidding and a budget no other campaign uses. Adstack sets these when it creates a campaign. If the network or location-targeting settings drift, the next plan restores them; if the bidding strategy changes or the budget becomes shared, planning fails rather than converting the campaign.

## Ad groups

```yaml
ad_groups:
  - id: coffee-subscription
    ad_template: subscription     # or `ad:` with an inline ad
    cpc: 1.20                     # optional; defaults to the campaign's default_cpc
    keywords: ['[coffee subscription]', '[monthly coffee delivery]']
```

| Key | Description |
| --- | --- |
| `id` | Required. Unique within the campaign. The ad group's name in Google Ads is `adstack:<id>`. |
| `keywords` | Required. 1-500 exact-match keywords. A keyword may appear in only one ad group per campaign. |
| `ad_template` | The name of an entry in `ad_templates`. Set exactly one of `ad_template` and `ad`. |
| `ad` | An inline [responsive search ad](#responsive-search-ads). |
| `cpc` | The ad group's manual CPC bid. Defaults to the campaign's `default_cpc`. |

Each ad group has exactly one active responsive search ad. Adstack creates it, and when the ad's content changes it creates (or re-enables) a matching ad and pauses the one it supersedes; ads are never edited in place. Don't add other ads to managed ad groups: other active responsive search ads are paused, and active ads of other types block the plan.

## Keywords

Keywords use the same notation as the Google Ads interface:

| Syntax | Match type | Allowed in |
| --- | --- | --- |
| `[running shoes]` | Exact | ad groups and negative keywords |
| `"running shoes"` | Phrase | negative keywords |
| `running shoes` | Broad | negative keywords |

Quote keywords in YAML so the brackets and quotation marks are kept: `'[running shoes]'`, `'"free"'`. Keyword text is lowercased and its whitespace is normalized. It can have at most 80 characters and 10 words. Exact match on Google Ads still matches close variants of the keyword.

## Responsive search ads

| Key | Description |
| --- | --- |
| `final_url` | Required. An absolute `https://` URL. |
| `headlines` | Required. 3-15 headlines, up to 30 characters each, unique regardless of case. |
| `descriptions` | Required. 2-4 descriptions, up to 90 characters each, unique regardless of case. |

Google reviews ads for policy compliance after they are created. Adstack doesn't check policy, so a valid configuration can still produce a disapproved ad.

## Names and ownership

Adstack keeps no state file. A campaign's identity is its name:

- Campaigns are named `adstack:<namespace>:<campaign id>`, and ad groups inside them `adstack:<ad group id>`.
- Every campaign in the account whose name starts with `adstack:<namespace>:` belongs to the configuration. A serving campaign in the namespace that is no longer configured blocks the plan until you re-plan with `--prune` (which pauses it) or `destroy`.
- Campaigns outside the namespace are never read or changed. Adstack doesn't adopt campaigns with other names, and a campaign in the namespace with a different type, bidding strategy or a shared budget makes the plan fail rather than be converted.

Don't rename managed campaigns or ad groups in Google Ads, and don't use the `adstack:` prefix for anything else. See [How Adstack works](how-it-works.md) for what Adstack reconciles and what it deliberately leaves alone.
