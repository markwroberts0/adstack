import io
import json
import shutil
import sys

import pytest
from conftest import QUICKSTART

from adstack import cli


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A working directory containing a copy of the quickstart configuration in ./ads."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADSTACK_SANDBOX", raising=False)
    shutil.copytree(QUICKSTART, tmp_path / "ads")
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_init_validate_and_refusing_to_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "init", "demo")
    assert code == 0 and "Created demo/adstack.yaml" in out
    code, out, _ = run(capsys, "validate", "demo")
    assert code == 0 and "1 campaign, 1 ad group, 2 keywords" in out
    code, _, err = run(capsys, "init", "demo")
    assert code == 1 and "already exists" in err


def test_validate_reports_errors(project, capsys):
    (project / "ads" / "adstack.yaml").write_text("version: 1\nprovider: {}\ncampaigns: []\n")
    code, _, err = run(capsys, "validate", "ads")
    assert code == 1 and err.startswith("Error: provider: configure exactly one provider")
    code, out, _ = run(capsys, "validate", "ads", "--json")
    assert code == 1 and json.loads(out)["valid"] is False


def test_plan_save_show_apply_and_detailed_exit_codes(project, capsys):
    code, out, err = run(capsys, "plan", "ads", "--sandbox", "--out", "first.plan.json", "--detailed-exitcode")
    assert code == 2
    assert "+ campaign running-shoes" in out and "Plan: 15 to add, 0 to change, 0 to remove." in out
    assert "adstack apply first.plan.json" in err

    code, out, _ = run(capsys, "show", "first.plan.json")
    assert code == 0 and "Plan: 15 to add" in out and "adstack.yaml" in out
    code, out, _ = run(capsys, "show", "first.plan.json", "--json")
    assert json.loads(out)["summary"] == {"add": 15, "change": 0, "remove": 0}

    code, _, err = run(capsys, "plan", "ads", "--sandbox", "--out", "first.plan.json")
    assert code == 1 and "never overwritten" in err

    code, out, _ = run(capsys, "apply", "first.plan.json")
    assert code == 0 and "Apply complete! Resources: 15 added, 0 changed, 0 removed." in out
    assert len(list((project / ".adstack" / "receipts").iterdir())) == 1

    code, out, _ = run(capsys, "plan", "ads", "--sandbox", "--detailed-exitcode")
    assert code == 0 and "No changes." in out
    code, _, err = run(capsys, "apply", "first.plan.json")
    assert code == 1 and "changed after this plan was created" in err


def test_interactive_apply_needs_yes(project, capsys, monkeypatch):
    code, _, err = run(capsys, "apply", "ads", "--sandbox")
    assert code == 1 and "needs approval" in err  # stdin is not a terminal

    monkeypatch.setattr(sys, "stdin", Terminal("no\n"))
    code, out, err = run(capsys, "apply", "ads", "--sandbox")
    assert code == 1 and "Only 'yes' will be accepted" in out and "Apply cancelled." in err

    monkeypatch.setattr(sys, "stdin", Terminal("yes\n"))
    code, out, _ = run(capsys, "apply", "ads", "--sandbox")
    assert code == 0 and "15 added" in out


def test_permissions_are_checked_before_approval(project, capsys):
    assert run(capsys, "apply", "ads", "--sandbox", "--auto-approve")[0] == 0
    config = project / "ads" / "adstack.yaml"
    config.write_text(config.read_text().replace("status: PAUSED", "status: ENABLED"))
    code, out, err = run(capsys, "apply", "ads", "--sandbox", "--auto-approve")
    assert code == 1 and "--allow-enable" in out and "add --allow-enable" in err
    code, out, _ = run(capsys, "apply", "ads", "--sandbox", "--auto-approve", "--allow-enable")
    assert code == 0 and "0 added, 1 changed" in out


def test_blocked_plans_fail_and_prune_resolves_them(project, capsys):
    assert run(capsys, "apply", "ads", "--sandbox", "--auto-approve")[0] == 0
    config = project / "ads" / "adstack.yaml"
    config.write_text(config.read_text().replace("          - '[waterproof trail shoes]'\n", ""))
    code, out, err = run(capsys, "plan", "ads", "--sandbox", "--out", "blocked.json")
    assert code == 1 and "re-plan with --prune to pause it" in out and "the plan is blocked" in err
    assert not (project / "blocked.json").exists()
    code, out, _ = run(capsys, "apply", "ads", "--sandbox", "--prune", "--auto-approve")
    assert code == 0 and "0 added, 1 changed" in out


def test_state_report_and_destroy(project, capsys):
    assert run(capsys, "apply", "ads", "--sandbox", "--auto-approve")[0] == 0
    code, out, _ = run(capsys, "state", "list", "ads", "--sandbox")
    assert code == 0
    assert "campaign running-shoes  [PAUSED]  daily budget 25.00 USD" in out
    assert "    keyword [trail running shoes]  [ENABLED]" in out
    code, out, _ = run(capsys, "state", "list", "ads", "--sandbox", "--json")
    assert len(json.loads(out)["resources"]) == 14
    code, out, _ = run(capsys, "report", "performance", "ads", "--sandbox", "--days", "30")
    assert code == 0 and "adstack:example:running-shoes" in out and "impressions" in out

    code, out, _ = run(capsys, "destroy", "ads", "--sandbox", "--auto-approve")
    assert code == 0 and "No changes." in out  # everything is already paused


def test_saved_sandbox_plans_cannot_be_combined_with_options(project, capsys):
    assert run(capsys, "plan", "ads", "--sandbox", "--out", "p.json")[0] == 0
    code, _, err = run(capsys, "apply", "p.json", "--prune")
    assert code == 1 and "can't be used with a saved plan" in err


def test_real_platform_without_credentials_fails_cleanly(project, capsys, monkeypatch):
    for name in ("GOOGLE_ADS_CUSTOMER_ID", "GOOGLE_ADS_ACCESS_TOKEN", "GOOGLE_ADS_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    code, _, err = run(capsys, "plan", "ads")
    assert code == 1 and "GOOGLE_ADS_CUSTOMER_ID" in err
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "123-456-7890")
    code, _, err = run(capsys, "plan", "ads")
    assert code == 1 and "credentials are not set" in err


def test_version(capsys):
    code, out, _ = run(capsys, "version")
    assert code == 0 and out.startswith("adstack ")
