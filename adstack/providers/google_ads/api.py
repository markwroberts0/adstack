"""Google Ads REST API backend: OAuth, GAQL search, atomic mutate and reports.

Every request goes to ``customers/<account>`` and every mutate uses
``partialFailure=false``, so a request either applies completely or not at all.
Mutations are never retried automatically: after a network failure the outcome
is unknown, and the only safe next step is to read the account again.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import requests

from ... import __version__
from ...errors import ConfigError, ProviderError
from .schema import PREFIX, customer_id, format_customer_id

API_VERSION = "v26"
BASE_URL = f"https://googleads.googleapis.com/{API_VERSION}"
TOKEN_URL = "https://oauth2.googleapis.com/token"

ENV_CUSTOMER_ID = "GOOGLE_ADS_CUSTOMER_ID"
ENV_LOGIN_CUSTOMER_ID = "GOOGLE_ADS_LOGIN_CUSTOMER_ID"
ENV_ACCESS_TOKEN = "GOOGLE_ADS_ACCESS_TOKEN"
ENV_CLIENT_ID = "GOOGLE_ADS_CLIENT_ID"
ENV_CLIENT_SECRET = "GOOGLE_ADS_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "GOOGLE_ADS_REFRESH_TOKEN"

REPORT_KINDS = ("performance", "search_terms")
REPORT_DAYS = (7, 14, 30)

Search = Callable[[str], list[dict[str, Any]]]


def resolve_account(desired: dict[str, Any], env: Mapping[str, str]) -> str:
    """The account comes from the configuration, or else from GOOGLE_ADS_CUSTOMER_ID. They must agree."""
    configured = desired.get("customer_id")
    from_env = customer_id(env[ENV_CUSTOMER_ID], ENV_CUSTOMER_ID) if env.get(ENV_CUSTOMER_ID) else None
    if configured and from_env and configured != from_env:
        raise ConfigError(
            f"The configuration targets account {format_customer_id(configured)}, but {ENV_CUSTOMER_ID} is "
            f"{format_customer_id(from_env)}; unset {ENV_CUSTOMER_ID} or change provider.google_ads.customer_id"
        )
    account = configured or from_env
    if not account:
        raise ConfigError(f"Set provider.google_ads.customer_id in the configuration, or {ENV_CUSTOMER_ID}")
    return account


def resolve_login_customer(desired: dict[str, Any], env: Mapping[str, str]) -> str | None:
    if desired.get("login_customer_id"):
        return desired["login_customer_id"]
    value = env.get(ENV_LOGIN_CUSTOMER_ID)
    return customer_id(value, ENV_LOGIN_CUSTOMER_ID) if value else None


class GoogleAdsClient:
    """A minimal client for the Google Ads REST interface."""

    def __init__(
        self,
        account: str,
        *,
        env: Mapping[str, str],
        login_customer_id: str | None = None,
        session: requests.Session | None = None,
    ):
        self.account = account
        self.http = session or requests.Session()
        token = env.get(ENV_ACCESS_TOKEN)
        if not token:
            credentials = {
                "client_id": env.get(ENV_CLIENT_ID),
                "client_secret": env.get(ENV_CLIENT_SECRET),
                "refresh_token": env.get(ENV_REFRESH_TOKEN),
            }
            if not all(credentials.values()):
                raise ProviderError(
                    f"Google Ads credentials are not set: set {ENV_ACCESS_TOKEN}, or {ENV_CLIENT_ID}, "
                    f"{ENV_CLIENT_SECRET} and {ENV_REFRESH_TOKEN}"
                )
            token = self._refresh(credentials)
        self.http.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": f"adstack/{__version__}",
            }
        )
        if login_customer_id:
            self.http.headers["login-customer-id"] = login_customer_id

    def _refresh(self, credentials: dict[str, Any]) -> str:
        try:
            response = self.http.post(TOKEN_URL, data={**credentials, "grant_type": "refresh_token"}, timeout=30)
            response.raise_for_status()
            return response.json()["access_token"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise ProviderError(
                "OAuth token refresh failed; check the Google Ads OAuth credentials and their access to the account"
            ) from exc

    def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post(f"{BASE_URL}/customers/{self.account}/{endpoint}", json=body, timeout=60)
        except requests.RequestException as exc:
            raise ProviderError(
                f"{endpoint}: network failure. If this was a mutate, its outcome is unknown: do not retry blindly, "
                "run plan again to see the account's current state."
            ) from exc
        if not response.ok:
            request_id = response.headers.get("request-id", "unknown")
            raise ProviderError(
                f"{endpoint}: HTTP {response.status_code} (request-id {request_id})\n{response.text[:12000]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{endpoint}: the response was not JSON") from exc

    def search(self, query: str) -> list[dict[str, Any]]:
        """Run a GAQL query and return every row, following page tokens."""
        rows: list[dict[str, Any]] = []
        page = None
        while True:
            body = {"query": query}
            if page:
                body["pageToken"] = page
            data = self.post("googleAds:search", body)
            rows.extend(data.get("results", []))
            page = data.get("nextPageToken")
            if not page:
                return rows

    def mutate(self, operations: list[dict[str, Any]], *, validate_only: bool) -> dict[str, Any]:
        return self.post(
            "googleAds:mutate",
            {"mutateOperations": operations, "partialFailure": False, "validateOnly": validate_only},
        )


class ApiBackend:
    """The real Google Ads account."""

    sandbox = False

    def __init__(self, client: GoogleAdsClient, namespace: str):
        self.client = client
        self.account = client.account
        self.namespace = namespace

    @classmethod
    def connect(cls, desired: dict[str, Any], env: Mapping[str, str]) -> ApiBackend:
        account = resolve_account(desired, env)
        login = resolve_login_customer(desired, env)
        return cls(GoogleAdsClient(account, env=env, login_customer_id=login), desired["namespace"])

    def read(self) -> dict[str, Any]:
        return snapshot(self.client.search, self.account, self.namespace)

    def mutate(self, operations: list[dict[str, Any]], *, validate_only: bool) -> dict[str, Any]:
        return self.client.mutate(operations, validate_only=validate_only)

    def report(self, kind: str, days: int, *, all_campaigns: bool = False) -> list[dict[str, Any]]:
        return report(self.client.search, kind, days, namespace=None if all_campaigns else self.namespace)


def _resource(value: Any, collection: str, account: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"customers/{account}/{collection}/[0-9]+", value):
        raise ProviderError(f"unexpected {collection} resource name in the API response: {value!r}")
    return value


def _quoted(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _normalize_ad(row: dict[str, Any]) -> dict[str, Any]:
    """Keep only ad fields Adstack compares, so review metadata does not change the state hash."""
    ad = dict(row.get("ad", {}))
    rsa = ad.get("responsiveSearchAd")
    if isinstance(rsa, dict):
        ad["responsiveSearchAd"] = {
            field: [
                {key: asset[key] for key in ("text", "pinnedField") if key in asset} for asset in rsa.get(field, [])
            ]
            for field in ("headlines", "descriptions")
        }
    return {**row, "ad": ad}


def snapshot(search: Search, account: str, namespace: str) -> dict[str, Any]:
    """Read every campaign in the namespace and the resources Adstack manages under each one."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", namespace):
        raise ConfigError(f"invalid namespace {namespace!r}")
    customer = search("SELECT customer.id, customer.currency_code FROM customer")
    if len(customer) != 1 or str(customer[0]["customer"]["id"]) != account:
        raise ProviderError(f"Could not verify access to account {format_customer_id(account)}")
    state: dict[str, Any] = {"currency": customer[0]["customer"]["currencyCode"], "campaigns": {}}

    prefix = f"{PREFIX}:{namespace}:"
    rows = search(
        "SELECT campaign.resource_name, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign.campaign_budget, campaign.bidding_strategy_type, "
        "campaign.network_settings.target_google_search, campaign.network_settings.target_search_network, "
        "campaign.network_settings.target_content_network, "
        "campaign.network_settings.target_partner_search_network, "
        "campaign.geo_target_type_setting.positive_geo_target_type "
        f"FROM campaign WHERE campaign.name LIKE '{prefix}%' AND campaign.status != 'REMOVED'"
    )
    campaigns: dict[str, dict[str, Any]] = {}
    for row in rows:
        campaign = row["campaign"]
        if not campaign.get("name", "").startswith(prefix):
            continue
        _resource(campaign["resourceName"], "campaigns", account)
        _resource(campaign.get("campaignBudget"), "campaignBudgets", account)
        if campaign["name"] in campaigns:
            raise ProviderError(f"More than one campaign is named {campaign['name']!r}")
        campaigns[campaign["name"]] = {
            "campaign": campaign,
            "budget": None,
            "groups": [],
            "keywords": [],
            "ads": [],
            "criteria": [],
        }
    if not campaigns:
        return state

    by_resource = {entry["campaign"]["resourceName"]: entry for entry in campaigns.values()}
    budget_names = sorted({entry["campaign"]["campaignBudget"] for entry in campaigns.values()})
    budgets = {
        row["campaignBudget"]["resourceName"]: row["campaignBudget"]
        for row in search(
            "SELECT campaign_budget.resource_name, campaign_budget.amount_micros, "
            "campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign_budget.period "
            f"FROM campaign_budget WHERE campaign_budget.resource_name IN ({_quoted(budget_names)})"
        )
    }
    for entry in campaigns.values():
        entry["budget"] = budgets.get(entry["campaign"]["campaignBudget"])
        if entry["budget"] is None:
            raise ProviderError(f"Could not read the budget of campaign {entry['campaign']['name']!r}")

    scope = f"campaign.resource_name IN ({_quoted(sorted(by_resource))})"
    queries = {
        "groups": (
            "adGroup",
            (
                "SELECT campaign.resource_name, ad_group.resource_name, ad_group.name, ad_group.status, "
                f"ad_group.type, ad_group.cpc_bid_micros FROM ad_group WHERE {scope} AND ad_group.status != 'REMOVED'"
            ),
        ),
        "keywords": (
            "adGroupCriterion",
            (
                "SELECT campaign.resource_name, ad_group_criterion.resource_name, ad_group_criterion.ad_group, "
                "ad_group_criterion.status, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
                f"ad_group_criterion.negative FROM ad_group_criterion WHERE {scope} "
                "AND ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' "
                "AND ad_group.status != 'REMOVED'"
            ),
        ),
        "ads": (
            "adGroupAd",
            (
                "SELECT campaign.resource_name, ad_group_ad.resource_name, ad_group_ad.ad_group, ad_group_ad.status, "
                "ad_group_ad.ad.type, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, "
                f"ad_group_ad.ad.responsive_search_ad.descriptions FROM ad_group_ad WHERE {scope} "
                "AND ad_group_ad.status != 'REMOVED' AND ad_group.status != 'REMOVED'"
            ),
        ),
        "criteria": (
            "campaignCriterion",
            (
                "SELECT campaign.resource_name, campaign_criterion.resource_name, campaign_criterion.status, "
                "campaign_criterion.type, campaign_criterion.negative, "
                "campaign_criterion.location.geo_target_constant, "
                "campaign_criterion.keyword.text, campaign_criterion.keyword.match_type FROM campaign_criterion "
                f"WHERE {scope} AND campaign_criterion.type IN ('LOCATION', 'KEYWORD') "
                "AND campaign_criterion.status != 'REMOVED'"
            ),
        ),
    }
    for key, (field, query) in queries.items():
        for row in search(query):
            entry = by_resource.get(row.get("campaign", {}).get("resourceName"))
            if entry is None:
                raise ProviderError(f"unexpected campaign in {field} results")
            entry[key].append(_normalize_ad(row[field]) if key == "ads" else row[field])
    for entry in campaigns.values():
        for key in queries:
            entry[key].sort(key=lambda resource: resource["resourceName"])
    state["campaigns"] = dict(sorted(campaigns.items()))
    return state


def _metrics(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics", {})
    return {
        "impressions": int(metrics.get("impressions", 0)),
        "clicks": int(metrics.get("clicks", 0)),
        "cost": f"{int(metrics.get('costMicros', 0)) / 1_000_000:.2f}",
        "conversions": float(metrics.get("conversions", 0)),
    }


def report(search: Search, kind: str, days: int, *, namespace: str | None = None) -> list[dict[str, Any]]:
    """Performance or search-term rows for the last 7, 14 or 30 days. Cost is in account currency."""
    if kind not in REPORT_KINDS:
        raise ConfigError(f"unknown report {kind!r} (choose from {', '.join(REPORT_KINDS)})")
    if days not in REPORT_DAYS:
        raise ConfigError("days must be 7, 14 or 30")
    scope = f" AND campaign.name LIKE '{PREFIX}:{namespace}:%'" if namespace else ""
    if kind == "performance":
        rows = search(
            "SELECT campaign.name, campaign.status, metrics.impressions, metrics.clicks, metrics.cost_micros, "
            f"metrics.conversions FROM campaign WHERE segments.date DURING LAST_{days}_DAYS{scope} "
            "ORDER BY metrics.cost_micros DESC"
        )
        return [
            {"campaign": row["campaign"]["name"], "status": row["campaign"].get("status"), **_metrics(row)}
            for row in rows
        ]
    rows = search(
        "SELECT campaign.name, ad_group.name, search_term_view.search_term, metrics.impressions, metrics.clicks, "
        "metrics.cost_micros, metrics.conversions FROM search_term_view "
        f"WHERE segments.date DURING LAST_{days}_DAYS{scope} ORDER BY metrics.cost_micros DESC"
    )
    return [
        {
            "campaign": row["campaign"]["name"],
            "ad_group": row.get("adGroup", {}).get("name"),
            "search_term": row.get("searchTermView", {}).get("searchTerm"),
            **_metrics(row),
        }
        for row in rows
    ]
