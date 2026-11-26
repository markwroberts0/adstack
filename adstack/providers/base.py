"""The interface between Adstack's workflow (plan, apply, MCP) and an ad platform."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class Backend(Protocol):
    """A connection to one ad account: the platform's real API, or a local sandbox."""

    account: str
    """The account ID that plans are bound to."""
    sandbox: bool

    def read(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of everything Adstack manages in the account."""
        ...

    def mutate(self, operations: list[dict[str, Any]], *, validate_only: bool) -> dict[str, Any]:
        """Apply operations atomically, or only ask the platform to validate them."""
        ...

    def report(self, kind: str, days: int, *, all_campaigns: bool = False) -> list[dict[str, Any]]:
        """Return performance rows (``kind`` is ``performance`` or ``search_terms``)."""
        ...


class Provider(Protocol):
    """An ad platform. Everything except ``connect`` is pure and makes no network requests."""

    name: str
    """Configuration key, for example ``google_ads``."""
    title: str
    """Human-readable platform name, for example ``Google Ads``."""

    def load(self, settings: Any, document: dict[str, Any]) -> dict[str, Any]:
        """Validate the provider block and the rest of the configuration; return the desired state."""
        ...

    def summary(self, desired: dict[str, Any]) -> str:
        """One line describing the desired state, for example ``2 campaigns, 5 ad groups``."""
        ...

    def connect(self, desired: dict[str, Any], *, sandbox_dir: Path | None, env: Mapping[str, str]) -> Backend:
        """Open the configured account, or the local sandbox when ``sandbox_dir`` is set."""
        ...

    def plan(
        self, desired: dict[str, Any], state: dict[str, Any], account: str, *, prune: bool, destroy: bool
    ) -> dict[str, Any]:
        """Compute ``changes``, ``blockers``, ``requires`` and ``operations``. Must be deterministic."""
        ...

    def inventory(self, desired: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten a snapshot into records with resource, campaign, ad_group, name, status and details."""
        ...

    def format_account(self, account: str) -> str:
        """Display form of an account ID."""
        ...
