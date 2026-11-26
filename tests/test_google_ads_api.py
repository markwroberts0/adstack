"""The REST backend, tested against canned GAQL rows and a fake HTTP session (no network)."""

import pytest
import requests

from adstack.errors import ConfigError, ProviderError
from adstack.providers.google_ads import api

ACCOUNT = "1234567890"
CAMPAIGN = f"customers/{ACCOUNT}/campaigns/11"
BUDGET = f"customers/{ACCOUNT}/campaignBudgets/21"
GROUP = f"customers/{ACCOUNT}/adGroups/31"


def canned(rows_by_resource):
    """A search() that answers by the FROM clause, and records every query."""
    queries = []

    def search(query):
        queries.append(query)
        resource = query.split(" FROM ", 1)[1].split()[0]
        return rows_by_resource.get(resource, [])

    return search, queries


def account_rows(**overrides):
    campaign = {
        "resourceName": CAMPAIGN,
        "name": "adstack:shop:search",
        "status": "PAUSED",
        "advertisingChannelType": "SEARCH",
        "campaignBudget": BUDGET,
        "biddingStrategyType": "MANUAL_CPC",
        "networkSettings": {"targetGoogleSearch": True},
        "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
    }
    rows = {
        "customer": [{"customer": {"id": ACCOUNT, "currencyCode": "USD"}}],
        "campaign": [{"campaign": campaign}],
        "campaign_budget": [
            {
                "campaignBudget": {
                    "resourceName": BUDGET,
                    "amountMicros": "10000000",
                    "explicitlyShared": False,
                    "referenceCount": "1",
                    "period": "DAILY",
                }
            }
        ],
        "ad_group": [
            {
                "campaign": {"resourceName": CAMPAIGN},
                "adGroup": {
                    "resourceName": GROUP,
                    "name": "adstack:main",
                    "status": "ENABLED",
                    "type": "SEARCH_STANDARD",
                    "cpcBidMicros": "1000000",
                },
            }
        ],
        "ad_group_ad": [
            {
                "campaign": {"resourceName": CAMPAIGN},
                "adGroupAd": {
                    "resourceName": f"customers/{ACCOUNT}/adGroupAds/31~41",
                    "adGroup": GROUP,
                    "status": "ENABLED",
                    "ad": {
                        "type": "RESPONSIVE_SEARCH_AD",
                        "finalUrls": ["https://www.example.com/"],
                        "responsiveSearchAd": {
                            "headlines": [
                                {
                                    "text": "One",
                                    "assetPerformanceLabel": "LEARNING",
                                    "policySummaryInfo": {"reviewStatus": "REVIEWED"},
                                }
                            ],
                            "descriptions": [{"text": "First.", "pinnedField": "DESCRIPTION_1"}],
                        },
                    },
                },
            }
        ],
    }
    rows.update(overrides)
    return rows


def test_snapshot_reads_the_namespace_and_groups_rows_by_campaign():
    search, queries = canned(account_rows())
    state = api.snapshot(search, ACCOUNT, "shop")
    assert "campaign.name LIKE 'adstack:shop:%'" in queries[1]
    assert all(f"campaign.resource_name IN ('{CAMPAIGN}')" in q for q in queries[3:])
    entry = state["campaigns"]["adstack:shop:search"]
    assert state["currency"] == "USD"
    assert entry["budget"]["amountMicros"] == "10000000"
    assert [g["name"] for g in entry["groups"]] == ["adstack:main"]
    assert entry["keywords"] == [] and entry["criteria"] == []
    # Review and performance metadata must not leak into the state hash.
    assert entry["ads"][0]["ad"]["responsiveSearchAd"] == {
        "headlines": [{"text": "One"}],
        "descriptions": [{"text": "First.", "pinnedField": "DESCRIPTION_1"}],
    }


def test_snapshot_of_an_empty_namespace_makes_two_queries():
    search, queries = canned(account_rows(campaign=[]))
    assert api.snapshot(search, ACCOUNT, "shop") == {"currency": "USD", "campaigns": {}}
    assert len(queries) == 2


def test_snapshot_fails_closed_on_unexpected_responses():
    search, _ = canned(account_rows(customer=[{"customer": {"id": "999", "currencyCode": "USD"}}]))
    with pytest.raises(ProviderError, match="Could not verify access"):
        api.snapshot(search, ACCOUNT, "shop")
    rows = account_rows()
    rows["campaign"][0]["campaign"]["resourceName"] = "customers/5555555555/campaigns/11"
    with pytest.raises(ProviderError, match="unexpected campaigns resource name"):
        api.snapshot(canned(rows)[0], ACCOUNT, "shop")
    rows = account_rows()
    rows["campaign"].append(rows["campaign"][0])
    with pytest.raises(ProviderError, match="More than one campaign"):
        api.snapshot(canned(rows)[0], ACCOUNT, "shop")


def test_reports_convert_micros_and_scope_to_the_namespace():
    search, queries = canned(
        {
            "campaign": [
                {
                    "campaign": {"name": "adstack:shop:search", "status": "ENABLED"},
                    "metrics": {"impressions": "120", "clicks": "7", "costMicros": "4567890", "conversions": 1.5},
                }
            ]
        }
    )
    rows = api.report(search, "performance", 30, namespace="shop")
    assert rows == [
        {
            "campaign": "adstack:shop:search",
            "status": "ENABLED",
            "impressions": 120,
            "clicks": 7,
            "cost": "4.57",
            "conversions": 1.5,
        }
    ]
    assert "LAST_30_DAYS AND campaign.name LIKE 'adstack:shop:%'" in queries[0]
    api.report(search, "performance", 7)
    assert "LIKE" not in queries[1]
    with pytest.raises(ConfigError):
        api.report(search, "performance", 9)


def test_account_comes_from_configuration_or_environment_and_they_must_agree():
    desired = {"customer_id": "1234567890", "login_customer_id": None}
    assert api.resolve_account(desired, {}) == "1234567890"
    assert api.resolve_account({"customer_id": None}, {api.ENV_CUSTOMER_ID: "123-456-7890"}) == "1234567890"
    with pytest.raises(ConfigError, match="but GOOGLE_ADS_CUSTOMER_ID is 999-999-9999"):
        api.resolve_account(desired, {api.ENV_CUSTOMER_ID: "9999999999"})
    with pytest.raises(ConfigError, match=r"Set provider\.google_ads\.customer_id"):
        api.resolve_account({"customer_id": None}, {})
    assert api.resolve_login_customer({"login_customer_id": None}, {api.ENV_LOGIN_CUSTOMER_ID: "111-222-3333"}) == (
        "1112223333"
    )


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.ok = status, status < 400
        self._body, self.headers, self.text = body, headers or {}, str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, *responses):
        self.headers, self.calls, self.responses = {}, [], list(responses)

    def post(self, url, json=None, data=None, timeout=None):
        self.calls.append({"url": url, "json": json, "data": data})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_client_uses_an_access_token_and_pages_through_results():
    session = FakeSession(
        FakeResponse(body={"results": [{"n": 1}], "nextPageToken": "p2"}),
        FakeResponse(body={"results": [{"n": 2}]}),
    )
    client = api.GoogleAdsClient(
        ACCOUNT, env={api.ENV_ACCESS_TOKEN: "token"}, login_customer_id="1112223333", session=session
    )
    assert client.search("SELECT x FROM y") == [{"n": 1}, {"n": 2}]
    assert session.headers["Authorization"] == "Bearer token"
    assert session.headers["login-customer-id"] == "1112223333"
    assert "developer-token" not in session.headers  # sunset on 2026-09-09
    assert session.calls[0]["url"] == f"{api.BASE_URL}/customers/{ACCOUNT}/googleAds:search"
    assert session.calls[1]["json"] == {"query": "SELECT x FROM y", "pageToken": "p2"}


def test_client_refreshes_oauth_tokens_and_mutates_atomically():
    env = {api.ENV_CLIENT_ID: "id", api.ENV_CLIENT_SECRET: "secret", api.ENV_REFRESH_TOKEN: "refresh"}
    session = FakeSession(FakeResponse(body={"access_token": "fresh"}), FakeResponse(body={}))
    client = api.GoogleAdsClient(ACCOUNT, env=env, session=session)
    assert session.calls[0]["url"] == api.TOKEN_URL and session.calls[0]["data"]["grant_type"] == "refresh_token"
    assert session.headers["Authorization"] == "Bearer fresh"
    client.mutate([{"campaignOperation": {}}], validate_only=True)
    assert session.calls[1]["json"] == {
        "mutateOperations": [{"campaignOperation": {}}],
        "partialFailure": False,
        "validateOnly": True,
    }


def test_client_errors_are_reported_without_retrying():
    with pytest.raises(ProviderError, match="credentials are not set"):
        api.GoogleAdsClient(ACCOUNT, env={}, session=FakeSession())
    session = FakeSession(FakeResponse(status=401, body={"error": "nope"}))
    with pytest.raises(ProviderError, match="OAuth token refresh failed"):
        api.GoogleAdsClient(
            ACCOUNT,
            env={api.ENV_CLIENT_ID: "a", api.ENV_CLIENT_SECRET: "b", api.ENV_REFRESH_TOKEN: "c"},
            session=session,
        )
    session = FakeSession(
        FakeResponse(status=400, body={"error": "bad"}, headers={"request-id": "abc"}),
        requests.ConnectionError("reset"),
    )
    client = api.GoogleAdsClient(ACCOUNT, env={api.ENV_ACCESS_TOKEN: "t"}, session=session)
    with pytest.raises(ProviderError, match=r"HTTP 400 \(request-id abc\)"):
        client.mutate([], validate_only=True)
    with pytest.raises(ProviderError, match="outcome is unknown"):
        client.mutate([], validate_only=False)
    assert len(session.calls) == 2
