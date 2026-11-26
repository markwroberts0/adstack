"""Google Ads provider: Search campaigns with manual CPC bidding, managed through the REST API."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import planner, schema
from .api import ApiBackend
from .sandbox import SandboxBackend


class GoogleAdsProvider:
    name = "google_ads"
    title = "Google Ads"

    def load(self, settings: Any, document: dict[str, Any]) -> dict[str, Any]:
        return schema.load(settings, document)

    def summary(self, desired: dict[str, Any]) -> str:
        return schema.summary(desired)

    def connect(
        self, desired: dict[str, Any], *, sandbox_dir: Path | None, env: Mapping[str, str]
    ) -> ApiBackend | SandboxBackend:
        if sandbox_dir is not None:
            return SandboxBackend(Path(sandbox_dir) / "google_ads.json", desired["namespace"], desired["currency"])
        return ApiBackend.connect(desired, env)

    def plan(
        self, desired: dict[str, Any], state: dict[str, Any], account: str, *, prune: bool, destroy: bool
    ) -> dict[str, Any]:
        return planner.plan(desired, state, account, prune=prune, destroy=destroy)

    def inventory(self, desired: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
        return planner.inventory(state, desired["currency"])

    def format_account(self, account: str) -> str:
        return schema.format_customer_id(account)
