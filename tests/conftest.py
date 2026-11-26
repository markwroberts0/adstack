from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from adstack import engine
from adstack.config import Config, load, load_sources

ROOT = Path(__file__).resolve().parents[1]
QUICKSTART = ROOT / "examples" / "quickstart"
MULTI_FILE = ROOT / "examples" / "multi-file"
ACCOUNT = "1234567890"

MINIMAL = """\
version: 1
provider:
  google_ads: {namespace: test, currency: USD}
ad_templates:
  basic:
    final_url: https://www.example.com/
    headlines: [Headline One, Headline Two, Headline Three]
    descriptions: [The first description., The second description.]
campaigns:
  - id: search
    status: PAUSED
    daily_budget: 10
    default_cpc: 1
    locations: [2840]
    negative_keywords: ['"jobs"']
    ad_groups:
      - id: main
        ad_template: basic
        keywords: ['[widgets]', '[blue widgets]']
"""


def config_text(text: str, name: str = "adstack.yaml") -> Config:
    return load_sources({name: textwrap.dedent(text)})


def quickstart() -> Config:
    return load(QUICKSTART)


def empty_state(currency: str = "USD") -> dict[str, Any]:
    return {"currency": currency, "campaigns": {}}


def find(backend: Any, collection: str, **match: Any) -> dict[str, Any]:
    """The single sandbox resource in ``collection`` whose fields equal ``match``."""
    rows = [
        row
        for row in backend.data["resources"][collection].values()
        if all(row.get(key) == value for key, value in match.items())
    ]
    assert len(rows) == 1, f"expected one {collection} matching {match}, found {len(rows)}"
    return rows[0]


def operation_bodies(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [next(iter(operation.values())) for operation in plan["operations"]]


@pytest.fixture
def workspace(tmp_path: Path) -> engine.Workspace:
    return engine.Workspace(root=tmp_path, sandbox=True, env={})


@pytest.fixture
def applied(workspace: engine.Workspace):
    """The quickstart configuration, applied to a fresh sandbox. Returns (config, backend)."""
    config = quickstart()
    result = engine.apply(engine.create_plan(config, workspace), workspace)
    assert result is not None
    return config, engine.connect(config, workspace)
