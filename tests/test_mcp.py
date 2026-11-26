import shutil

import anyio
import pytest
from conftest import QUICKSTART

pytest.importorskip("mcp")

from mcp import Client

from adstack.mcp_server import create_server


@pytest.fixture
def root(tmp_path):
    shutil.copytree(QUICKSTART, tmp_path / "ads")
    return tmp_path


def call(server, steps):
    """Run (tool, arguments) steps in one client session and return the results."""

    async def session():
        async with Client(server) as client:
            return [await client.call_tool(tool, arguments) for tool, arguments in steps]

    return anyio.run(session)


def tool_names(server):
    async def session():
        async with Client(server) as client:
            return sorted(tool.name for tool in (await client.list_tools()).tools)

    return anyio.run(session)


def test_apply_is_only_offered_for_the_sandbox_or_when_allowed(root):
    read_only = ["example_config", "plan", "report", "show_plan", "state", "validate"]
    assert tool_names(create_server(root, env={})) == read_only
    assert tool_names(create_server(root, allow_apply=True, env={})) == sorted([*read_only, "apply"])
    assert tool_names(create_server(root, sandbox=True, env={})) == sorted([*read_only, "apply"])


def test_validate_reports_problems_as_data(root):
    [ok, bad, both] = call(
        create_server(root, sandbox=True, env={}),
        [
            ("validate", {"path": "ads"}),
            (
                "validate",
                {"config": "version: 1\nprovider: {google_ads: {namespace: x, currency: usd}}\ncampaigns: []"},
            ),
            ("validate", {"path": "ads", "config": "version: 1"}),
        ],
    )
    assert ok.structured_content == {
        "valid": True,
        "provider": "google_ads",
        "summary": "1 campaign, 2 ad groups, 5 keywords",
    }
    assert bad.structured_content["valid"] is False and "ISO 4217" in bad.structured_content["error"]
    assert both.is_error and "either path or config" in both.content[0].text


def test_plan_review_apply_flow_in_the_sandbox(root):
    server = create_server(root, sandbox=True, env={})
    [planned] = call(server, [("plan", {"path": "ads"})])
    plan = planned.structured_content
    assert plan["summary"] == {"add": 15, "change": 0, "remove": 0} and plan["sandbox"]
    assert "+ campaign running-shoes" in plan["plan_text"]
    assert f"plan_id={plan['plan_id']}" in plan["next_step"]

    shown, wrong_account, applied, again = call(
        server,
        [
            ("show_plan", {"plan_id": plan["plan_id"]}),
            ("apply", {"plan_id": plan["plan_id"], "confirm_account": "123-456-7890"}),
            ("apply", {"plan_id": plan["plan_id"], "confirm_account": "000-000-0000"}),
            ("plan", {"path": "ads"}),
        ],
    )
    assert shown.structured_content["plan_text"] == plan["plan_text"]
    assert wrong_account.is_error and "does not match the plan's account" in wrong_account.content[0].text
    assert applied.structured_content["applied"] is True
    assert applied.structured_content["receipt"].startswith(".adstack/receipts/")
    assert again.structured_content["has_changes"] is False and again.structured_content["plan_id"] is None

    [stale] = call(server, [("apply", {"plan_id": plan["plan_id"], "confirm_account": "0000000000"})])
    assert stale.is_error and "changed after this plan was created" in stale.content[0].text


def test_inline_configuration_and_permissions(root):
    server = create_server(root, sandbox=True, env={})
    text = (root / "ads" / "adstack.yaml").read_text()
    [first] = call(server, [("plan", {"config": text})])
    call(server, [("apply", {"plan_id": first.structured_content["plan_id"], "confirm_account": "0000000000"})])
    [enable] = call(server, [("plan", {"config": text.replace("status: PAUSED", "status: ENABLED")})])
    plan = enable.structured_content
    assert plan["requires"] == ["allow_enable"] and "allow_enable=true" in plan["next_step"]
    denied, granted, state = call(
        server,
        [
            ("apply", {"plan_id": plan["plan_id"], "confirm_account": "0000000000"}),
            ("apply", {"plan_id": plan["plan_id"], "confirm_account": "0000000000", "allow_enable": True}),
            ("state", {"config": text}),
        ],
    )
    assert denied.is_error and "allow_enable" in denied.content[0].text
    assert granted.structured_content["summary"] == {"add": 0, "change": 1, "remove": 0}
    assert state.structured_content["resources"][0]["status"] == "ENABLED"


def test_paths_must_stay_inside_the_root_and_plan_ids_are_checked(root):
    escape, bad_id, missing = call(
        create_server(root / "ads", sandbox=True, env={}),
        [
            ("validate", {"path": "../.."}),
            ("show_plan", {"plan_id": "../../etc/passwd"}),
            ("show_plan", {"plan_id": "0123456789abcdef"}),
        ],
    )
    assert escape.is_error and "outside the server's root" in escape.content[0].text
    assert bad_id.is_error and "16-character ID" in bad_id.content[0].text
    assert missing.is_error and "no plan with ID" in missing.content[0].text


def test_reports_example_and_instructions(root):
    server = create_server(root, sandbox=True, env={})
    report, example = call(server, [("report", {"path": "ads", "days": 14}), ("example_config", {})])
    assert report.structured_content["rows"] == [] and report.structured_content["currency"] == "USD"
    assert example.structured_content["config"].startswith("# Adstack configuration")

    async def instructions(server):
        async with Client(server) as client:
            return client.instructions if isinstance(client.instructions, str) else client.instructions()

    sandbox_text = anyio.run(instructions, server)
    assert "sandbox" in sandbox_text and "Never call apply without" in sandbox_text
    real_text = anyio.run(instructions, create_server(root, env={}))
    assert "cannot apply changes" in real_text and "real money" in real_text
