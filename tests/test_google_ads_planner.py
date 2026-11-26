import copy

import pytest
from conftest import ACCOUNT, MULTI_FILE, empty_state, find, operation_bodies, quickstart

from adstack import engine
from adstack.config import load
from adstack.errors import PlanError
from adstack.providers.google_ads.planner import plan


def test_initial_plan_creates_everything_paused_exact_and_in_order():
    config = quickstart()
    result = plan(config.desired, empty_state(), ACCOUNT)
    # 1 budget + 1 campaign + 1 location + 3 negatives + 2 ad groups + 5 keywords + 2 ads
    assert len(result["operations"]) == 15
    assert len(result["changes"]) == 15
    assert not result["blockers"] and not any(result["requires"].values())
    created: set[str] = set()
    for operation in result["operations"]:
        ((kind, body),) = operation.items()
        row = body["create"]
        for field in ("campaign", "adGroup", "campaignBudget"):
            if field in row:
                assert row[field] in created, f"{kind} references {row[field]} before it is created"
        if "resourceName" in row:
            assert row["resourceName"] not in created
            assert row["resourceName"].rsplit("/", 1)[1].startswith("-")
            created.add(row["resourceName"])
        if kind == "campaignOperation":
            assert row["status"] == "PAUSED"
            assert row["networkSettings"] == {
                "targetGoogleSearch": True,
                "targetSearchNetwork": False,
                "targetContentNetwork": False,
                "targetPartnerSearchNetwork": False,
            }
            assert row["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
            assert row["name"] == "adstack:example:running-shoes"
        if kind == "adGroupCriterionOperation":
            assert row["keyword"]["matchType"] == "EXACT" and "[" not in row["keyword"]["text"]
    temp_ids = [name.rsplit("/", 1)[1] for name in created]
    assert len(temp_ids) == len(set(temp_ids)), "temporary IDs must be unique across the request"


def test_replan_after_apply_is_a_no_op(applied, workspace):
    config, backend = applied
    again = engine.create_plan(config, workspace, backend=backend)
    assert not again["operations"] and not again["changes"] and not again["blockers"]


def test_missing_keyword_and_creative_change(applied):
    config, backend = applied
    keyword = find(backend, "adGroupCriteria", keyword={"text": "trail running shoes", "matchType": "EXACT"})
    del backend.data["resources"]["adGroupCriteria"][keyword["resourceName"]]
    desired = copy.deepcopy(config.desired)
    group = desired["campaigns"][0]["groups"][0]
    group["ad"]["responsiveSearchAd"]["headlines"][0]["text"] = "Running Shoes On Sale"
    result = plan(desired, backend.read(), backend.account)
    actions = sorted((c["action"], c["resource"], c.get("note")) for c in result["changes"])
    assert actions == [("create", "ad", None), ("create", "keyword", None), ("update", "ad", "superseded")]
    assert len(result["operations"]) == 3


def test_prune_is_explicit(applied):
    config, backend = applied
    desired = copy.deepcopy(config.desired)
    desired["campaigns"][0]["groups"][0]["keywords"].pop()
    desired["campaigns"][0]["negatives"].pop()
    blocked = plan(desired, backend.read(), backend.account)
    assert len(blocked["blockers"]) == 2 and not blocked["operations"]
    assert all("--prune" in blocker for blocker in blocked["blockers"])
    pruned = plan(desired, backend.read(), backend.account, prune=True)
    assert not pruned["blockers"]
    assert sorted(next(iter(b)) for b in operation_bodies(pruned)) == ["remove", "update"]


def test_enable_and_increases_need_permission_and_new_campaigns_start_paused(applied):
    config, backend = applied
    desired = copy.deepcopy(config.desired)
    desired["campaigns"][0]["status"] = "ENABLED"
    desired["campaigns"][0]["budget"] += 1_000_000
    result = plan(desired, backend.read(), backend.account)
    assert result["requires"] == {"allow_enable": True, "allow_budget_increase": True, "allow_live_changes": False}

    find(backend, "campaigns")["status"] = "ENABLED"
    assert plan(desired, backend.read(), backend.account)["requires"]["allow_live_changes"]

    with pytest.raises(PlanError, match="created PAUSED"):
        plan(desired, empty_state(), ACCOUNT)


def test_currency_and_shared_budget_fail_closed(applied):
    config, backend = applied
    state = backend.read()
    with pytest.raises(PlanError, match="currency"):
        plan(config.desired, {**state, "currency": "EUR"}, backend.account)
    budget = next(iter(state["campaigns"].values()))["budget"]
    budget["explicitlyShared"] = True
    with pytest.raises(PlanError, match="dedicated daily budget"):
        plan(config.desired, state, backend.account)


def test_existing_campaign_with_other_bidding_is_not_adopted(applied):
    config, backend = applied
    find(backend, "campaigns")["biddingStrategyType"] = "TARGET_SPEND"
    with pytest.raises(PlanError, match="will not adopt"):
        plan(config.desired, backend.read(), backend.account)


def test_one_operation_per_resource(applied, workspace):
    # Google rejects a request that mutates the same resource twice (ID_EXISTS_IN_MULTIPLE_MUTATES).
    config, backend = applied
    campaign = find(backend, "campaigns")
    campaign["networkSettings"]["targetContentNetwork"] = True
    campaign["geoTargetTypeSetting"]["positiveGeoTargetType"] = "PRESENCE_OR_INTEREST"
    group = find(backend, "adGroups", name="adstack:road")
    group["status"] = "PAUSED"
    backend.save()
    desired = copy.deepcopy(config.desired)
    desired["campaigns"][0]["status"] = "ENABLED"
    desired["campaigns"][0]["groups"][0]["bid"] += 250_000

    result = plan(desired, backend.read(), backend.account)
    updates = {body["update"]["resourceName"]: body for body in operation_bodies(result)}
    assert len(result["operations"]) == len(updates) == 2
    assert updates[group["resourceName"]]["updateMask"] == "cpcBidMicros,status"
    assert updates[campaign["resourceName"]]["updateMask"] == (
        "networkSettings.targetContentNetwork,geoTargetTypeSetting.positiveGeoTargetType,status"
    )
    assert updates[campaign["resourceName"]]["update"] == {
        "resourceName": campaign["resourceName"],
        "networkSettings": {"targetContentNetwork": False},
        "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
        "status": "ENABLED",
    }
    by_resource = {change["resource"]: change for change in result["changes"]}
    assert set(by_resource["campaign"]["diff"]) == {"target_content_network", "positive_geo_target_type", "status"}
    assert set(by_resource["ad_group"]["diff"]) == {"cpc_bid", "status"}
    # The sandbox enforces Google's rule, so the merged request must apply cleanly.
    backend.mutate(result["operations"], validate_only=True)


def test_orphan_campaign_blocks_until_pruned(workspace):
    config = load(MULTI_FILE)
    engine.apply(engine.create_plan(config, workspace), workspace)
    backend = engine.connect(config, workspace)
    find(backend, "campaigns", name="adstack:example-coffee:brand")["status"] = "ENABLED"
    desired = copy.deepcopy(config.desired)
    desired["campaigns"] = [c for c in desired["campaigns"] if c["id"] != "brand"]

    blocked = plan(desired, backend.read(), backend.account)
    assert blocked["blockers"] == [
        "campaign brand: exists in the account (ENABLED) but not in the configuration; re-plan with --prune to pause it"
    ]
    pruned = plan(desired, backend.read(), backend.account, prune=True)
    assert [(c["action"], c["resource"], c["campaign"], c["note"]) for c in pruned["changes"]] == [
        ("update", "campaign", "brand", "not in configuration")
    ]
    assert pruned["requires"]["allow_live_changes"]

    # A paused orphan is harmless and is left alone.
    find(backend, "campaigns", name="adstack:example-coffee:brand")["status"] = "PAUSED"
    assert not plan(desired, backend.read(), backend.account)["blockers"]


def test_destroy_pauses_only_this_namespace(applied, workspace):
    config, backend = applied
    other = load(MULTI_FILE)
    engine.apply(engine.create_plan(other, workspace), workspace)
    backend = engine.connect(config, workspace)
    for campaign in backend.data["resources"]["campaigns"].values():
        campaign["status"] = "ENABLED"
    result = plan(config.desired, backend.read(), backend.account, destroy=True)
    assert [(c["resource"], c["campaign"], c["diff"]) for c in result["changes"]] == [
        ("campaign", "running-shoes", {"status": ["ENABLED", "PAUSED"]})
    ]
    assert result["requires"] == {"allow_enable": False, "allow_budget_increase": False, "allow_live_changes": True}


def test_unmanaged_resources_in_managed_campaigns_block(applied):
    config, backend = applied
    resources = backend.data["resources"]
    campaign = find(backend, "campaigns")["resourceName"]
    road = find(backend, "adGroups", name="adstack:road")["resourceName"]
    resources["adGroups"]["customers/0000000000/adGroups/9001"] = {
        "resourceName": "customers/0000000000/adGroups/9001",
        "campaign": campaign,
        "name": "Manual group",
        "status": "ENABLED",
        "type": "SEARCH_STANDARD",
        "cpcBidMicros": "500000",
    }
    resources["adGroupCriteria"]["customers/0000000000/adGroupCriteria/9002~1"] = {
        "resourceName": "customers/0000000000/adGroupCriteria/9002~1",
        "adGroup": road,
        "status": "ENABLED",
        "keyword": {"text": "cheap", "matchType": "BROAD"},
        "negative": True,
    }
    resources["campaignCriteria"]["customers/0000000000/campaignCriteria/9003~1"] = {
        "resourceName": "customers/0000000000/campaignCriteria/9003~1",
        "campaign": campaign,
        "status": "ENABLED",
        "type": "LOCATION",
        "negative": False,
        "location": {"geoTargetConstant": "geoTargetConstants/2124"},
    }
    result = plan(config.desired, backend.read(), backend.account)
    assert len(result["blockers"]) == 3
    assert any("unmanaged active ad group 'Manual group'" in b for b in result["blockers"])
    assert any("unmanaged negative keyword cheap" in b for b in result["blockers"])
    assert any("location 2124 exists in the account but not in the configuration" in b for b in result["blockers"])
    pruned = plan(config.desired, backend.read(), backend.account, prune=True)
    assert len(pruned["blockers"]) == 2  # pruning never touches resources Adstack doesn't own
    assert [c["action"] for c in pruned["changes"]] == ["remove"]
