"""Ad platform providers.

A provider validates its part of the configuration, reads live state, and plans
changes. The core workflow (saved plans, approval, receipts, the MCP server) is
shared by every provider. See CONTRIBUTING.md for how to add one.
"""

from __future__ import annotations

from ..errors import ConfigError
from .base import Backend, Provider

PROVIDERS = ("google_ads",)

__all__ = ["PROVIDERS", "Backend", "Provider", "get_provider"]


def get_provider(name: str) -> Provider:
    if name == "google_ads":
        from .google_ads import GoogleAdsProvider

        return GoogleAdsProvider()
    raise ConfigError(f"provider: unknown provider {name!r} (available: {', '.join(PROVIDERS)})")
