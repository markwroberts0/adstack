import json

import pytest
from conftest import find, quickstart

from adstack import engine
from adstack.errors import ProviderError
from adstack.providers.google_ads.planner import plan
from adstack.providers.google_ads.sandbox import SandboxBackend


def fresh_plan(workspace):
    config = quickstart()
    backend = engine.connect(config, workspace)
    return config, backend, plan(config.desired, backend.read(), backend.account)


def test_validate_only_changes_nothing(workspace):
    _, backend, result = fresh_plan(workspace)
    response = backend.mutate(result["operations"], validate_only=True)
    assert len(response["mutateOperationResponses"]) == 15
    assert backend.read()["campaigns"] == {}
    assert not (workspace.data_dir / "sandbox" / "google_ads.json").exists()


def test_requests_are_atomic(workspace):
    _, backend, result = fresh_plan(workspace)
    broken = [*result["operations"], {"adGroupOperation": {"remove": "customers/0000000000/adGroups/404"}}]
    with pytest.raises(ProviderError, match="operation 15: RESOURCE_NOT_FOUND"):
        backend.mutate(broken, validate_only=False)
    assert backend.read()["campaigns"] == {}


def test_references_to_temporary_names_must_come_after_their_creation(workspace):
    _, backend, result = fresh_plan(workspace)
    reordered = [result["operations"][1], result["operations"][0], *result["operations"][2:]]
    with pytest.raises(ProviderError, match="operation 0: RESOURCE_NOT_FOUND"):
        backend.mutate(reordered, validate_only=True)


def test_one_request_may_not_mutate_a_resource_twice(applied):
    _, backend = applied
    campaign = find(backend, "campaigns")["resourceName"]
    operations = [
        {"campaignOperation": {"update": {"resourceName": campaign, "status": "ENABLED"}, "updateMask": "status"}},
        {
            "campaignOperation": {
                "update": {"resourceName": campaign, "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"}},
                "updateMask": "geoTargetTypeSetting.positiveGeoTargetType",
            }
        },
    ]
    with pytest.raises(ProviderError, match="ID_EXISTS_IN_MULTIPLE_MUTATES"):
        backend.mutate(operations, validate_only=True)


def test_state_persists_and_survives_reloading(applied, workspace):
    _, backend = applied
    reloaded = SandboxBackend(workspace.data_dir / "sandbox" / "google_ads.json", "example", "USD")
    assert reloaded.read() == backend.read()
    assert list(reloaded.read()["campaigns"]) == ["adstack:example:running-shoes"]
    other_namespace = SandboxBackend(workspace.data_dir / "sandbox" / "google_ads.json", "other", "USD")
    assert other_namespace.read()["campaigns"] == {}
    data = json.loads((workspace.data_dir / "sandbox" / "google_ads.json").read_text())
    assert data["format"] == "adstack-sandbox/1"


def test_duplicate_campaign_names_are_rejected(applied):
    config, backend = applied
    creates = plan(config.desired, {"currency": "USD", "campaigns": {}}, backend.account)["operations"]
    with pytest.raises(ProviderError, match="DUPLICATE_CAMPAIGN_NAME"):
        backend.mutate(creates, validate_only=True)


def test_reports_have_zero_metrics(applied):
    _, backend = applied
    assert backend.report("performance", 7) == [
        {
            "campaign": "adstack:example:running-shoes",
            "status": "PAUSED",
            "impressions": 0,
            "clicks": 0,
            "cost": "0.00",
            "conversions": 0.0,
        }
    ]
    assert backend.report("search_terms", 30) == []
