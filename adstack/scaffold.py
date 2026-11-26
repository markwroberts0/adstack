"""The annotated starter configuration written by ``adstack init`` and served by the MCP server."""

STARTER_CONFIG = """\
# Adstack configuration: https://github.com/markwroberts0/adstack/blob/main/docs/configuration.md
version: 1

provider:
  google_ads:
    # Adstack names campaigns adstack:<namespace>:<campaign id>. It manages every
    # campaign in its namespace and never touches campaigns outside it.
    namespace: example
    currency: USD                 # must match the Google Ads account's currency
    # customer_id: 123-456-7890   # pin the account here, or set GOOGLE_ADS_CUSTOMER_ID
    # limits:                     # guards against typos, in account currency (defaults shown)
    #   daily_budget: 1000
    #   cpc: 50

ad_templates:
  default:
    final_url: https://www.example.com/
    headlines:                    # 3-15 headlines, up to 30 characters each
      - Example Headline One
      - Example Headline Two
      - Example Headline Three
    descriptions:                 # 2-4 descriptions, up to 90 characters each
      - Describe what you offer and why it matters to the person searching.
      - Finish with a clear call to action, such as Start Your Free Trial Today.

campaigns:
  - id: search
    status: PAUSED                # new campaigns are always created PAUSED
    daily_budget: 10.00           # Google's average daily budget, in account currency
    default_cpc: 1.00             # manual CPC bid for ad groups that don't set cpc
    locations: [2840]             # geo target constant IDs (2840 = United States)
    negative_keywords:            # "phrase", [exact] or broad match
      - '"free"'
      - '"jobs"'
    ad_groups:
      - id: main
        ad_template: default      # or an inline ad: {final_url, headlines, descriptions}
        keywords:                 # exact match: [keyword]
          - '[example product]'
          - '[buy example product]'
"""
