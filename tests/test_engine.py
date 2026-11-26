import json

import pytest
from conftest import find, quickstart

from adstack import engine
from adstack import plan as plans
from adstack.errors import ConfigError, PlanError


def saved_copy(plan, tmp_path, name="p.json"):
    path = tmp_path / name
    plans.save(plan, path)
    return plans.load(path)


def test_saved_plans_verify_after_a_json_round_trip(workspace, tmp_path):
    plan = engine.create_plan(quickstart(), workspace)
    fresh, backend = engine.verify(saved_copy(plan, tmp_path), workspace)
    assert plans.body(fresh) == plans.body(plan)
    assert backend.sandbox and plan["backend"] == "sandbox"
    with pytest.raises(PlanError, match="never overwritten"):
        plans.save(plan, tmp_path / "p.json")


def test_plans_embed_their_configuration(workspace, tmp_path):
    plan = engine.create_plan(quickstart(), workspace)
    (tmp_path / "unrelated").mkdir()
    # Verifying needs only the plan: the configuration files can change or disappear.
    moved = engine.Workspace(root=workspace.root, sandbox=False, env={})
    fresh, _ = engine.verify(saved_copy(plan, tmp_path), moved)
    assert fresh["config"]["files"] == {"adstack.yaml": (quickstart().sources["adstack.yaml"])}


def test_tampered_plans_are_rejected(workspace, tmp_path):
    plan = engine.create_plan(quickstart(), workspace)
    edited = json.loads(json.dumps(plan))
    edited["config"]["files"]["adstack.yaml"] = edited["config"]["files"]["adstack.yaml"].replace("25.00", "250.00")
    with pytest.raises(PlanError, match="was modified"):
        engine.verify(edited, workspace)
    edited = json.loads(json.dumps(plan))
    edited["operations"][0]["campaignBudgetOperation"]["create"]["amountMicros"] = "999000000"
    with pytest.raises(PlanError, match="changed after this plan was created"):
        engine.verify(edited, workspace)
    edited = json.loads(json.dumps(plan))
    edited["format"] = "something-else"
    with pytest.raises(PlanError, match="not an Adstack plan"):
        engine.verify(edited, workspace)


def test_plans_go_stale_when_the_account_changes(applied, workspace):
    config, backend = applied
    plan = engine.create_plan(config, workspace)
    assert not plan["operations"]
    find(backend, "adGroups", name="adstack:trail")["status"] = "PAUSED"
    backend.save()
    with pytest.raises(PlanError, match="changed after this plan was created"):
        engine.apply(plan, workspace)


def test_apply_writes_a_receipt_and_respects_permissions(applied, workspace):
    config, backend = applied
    find(backend, "campaigns")["status"] = "ENABLED"
    find(backend, "adGroups", name="adstack:trail")["cpcBidMicros"] = "1000000"
    backend.save()
    plan = engine.create_plan(config, workspace)
    assert plan["requires"] == {"allow_enable": False, "allow_budget_increase": True, "allow_live_changes": True}
    with pytest.raises(PlanError, match=r"permission: allow_budget_increase \([^)]*\)$"):
        engine.apply(plan, workspace, allow={"allow_live_changes"})  # only the missing permission is listed
    receipt, path = engine.apply(plan, workspace, allow={"allow_budget_increase", "allow_live_changes"})
    assert path.parent == workspace.data_dir / "receipts"
    assert json.loads(path.read_text()) == receipt
    assert receipt["summary"] == {"add": 0, "change": 2, "remove": 0}  # pause campaign, raise trail bid
    assert receipt["plan_sha256"] == plans.plan_hash(plan)
    assert engine.apply(engine.create_plan(config, workspace), workspace) is None  # nothing left to do


def test_blocked_plans_cannot_be_applied(applied, workspace):
    _, backend = applied
    find(backend, "campaigns")["status"] = "ENABLED"
    backend.save()
    other = quickstart().sources["adstack.yaml"].replace("id: running-shoes", "id: renamed")
    renamed = engine.load_sources({"adstack.yaml": other})
    plan = engine.create_plan(renamed, workspace)
    assert plan["blockers"]
    with pytest.raises(PlanError, match="has blockers"):
        engine.apply(plan, workspace)


def test_real_platform_needs_an_account(tmp_path):
    workspace = engine.Workspace(root=tmp_path, sandbox=False, env={})
    with pytest.raises(ConfigError, match="GOOGLE_ADS_CUSTOMER_ID"):
        engine.create_plan(quickstart(), workspace)


def test_prune_and_destroy_are_exclusive(workspace):
    with pytest.raises(PlanError, match="cannot be combined"):
        engine.create_plan(quickstart(), workspace, prune=True, destroy=True)
