"""Validate the ``google_ads`` configuration and normalize it into a desired state.

This version manages Google Search campaigns with manual CPC bidding,
exact-match keywords, campaign negative keywords, location targeting and
responsive search ads. Validation is offline and strict: unknown keys,
duplicates and out-of-range values are errors.
"""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlsplit

from ...errors import ConfigError
from ...validation import distinct, mapping, money_micros, sequence, slug, text

PREFIX = "adstack"
"""Managed resource names start with this prefix: ``adstack:<namespace>:<campaign id>``."""

DEFAULT_LIMITS = {"daily_budget": "1000", "cpc": "50"}
"""Upper bounds (in account currency) that catch typos such as an extra zero. Override with ``limits``."""

_LIMIT_CEILING_MICROS = 1_000_000 * 1_000_000


def campaign_name(namespace: str, campaign_id: str) -> str:
    return f"{PREFIX}:{namespace}:{campaign_id}"


def ad_group_name(ad_group_id: str) -> str:
    return f"{PREFIX}:{ad_group_id}"


def customer_id(value: Any, source: str) -> str:
    """Normalize a Google Ads customer ID (``123-456-7890`` or ``1234567890``) to 10 digits."""
    digits = str(value).strip().replace("-", "") if isinstance(value, (str, int)) else ""
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{10}", digits):
        raise ConfigError(f"{source}: expected a 10-digit Google Ads customer ID such as 123-456-7890")
    return digits


def format_customer_id(value: str) -> str:
    return f"{value[:3]}-{value[3:6]}-{value[6:]}" if len(value) == 10 else value


def keyword(value: Any, path: str, *, exact_only: bool = False) -> dict[str, str]:
    """Parse ``[exact]``, ``"phrase"`` or ``broad`` keyword syntax."""
    value = text(value, path, maximum=82)
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        match_type = "EXACT"
    elif len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        match_type = "PHRASE"
    else:
        match_type = "BROAD"
    body = value if match_type == "BROAD" else value[1:-1]
    if exact_only and match_type != "EXACT":
        raise ConfigError(f"{path}: {value!r}: ad group keywords must use exact match, written [keyword]")
    if (
        not body.strip()
        or body != body.strip()
        or len(body) > 80
        or len(body.split()) > 10
        or any(c in body for c in '[]"')
    ):
        raise ConfigError(f"{path}: {value!r} is not a valid keyword (at most 80 characters and 10 words)")
    return {"text": " ".join(body.lower().split()), "matchType": match_type}


def keyword_label(keyword: dict[str, Any]) -> str:
    """Display a keyword in configuration syntax."""
    words, match_type = keyword.get("text", ""), keyword.get("matchType")
    if match_type == "EXACT":
        return f"[{words}]"
    if match_type == "PHRASE":
        return f'"{words}"'
    return str(words)


def responsive_search_ad(value: Any, path: str) -> dict[str, Any]:
    """Validate an ad definition and return it in Google Ads API shape."""
    ad = mapping(value, path, required={"final_url", "headlines", "descriptions"})
    headlines = [
        text(h, f"{path}.headlines", maximum=30)
        for h in sequence(ad["headlines"], f"{path}.headlines", minimum=3, maximum=15)
    ]
    descriptions = [
        text(d, f"{path}.descriptions", maximum=90)
        for d in sequence(ad["descriptions"], f"{path}.descriptions", minimum=2, maximum=4)
    ]
    distinct(headlines, f"{path}.headlines", str.casefold)
    distinct(descriptions, f"{path}.descriptions", str.casefold)
    url = text(ad["final_url"], f"{path}.final_url", maximum=2048)
    try:
        parts = urlsplit(url)
        valid = parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in url):
        raise ConfigError(f"{path}.final_url: expected an absolute https:// URL")
    return {
        "finalUrls": [url],
        "responsiveSearchAd": {
            "headlines": [{"text": h} for h in headlines],
            "descriptions": [{"text": d} for d in descriptions],
        },
    }


def load(settings: Any, document: dict[str, Any]) -> dict[str, Any]:
    """Validate the provider block and the campaigns; return the desired state."""
    settings = mapping(
        settings,
        "provider.google_ads",
        required={"namespace", "currency"},
        optional={"customer_id", "login_customer_id", "limits"},
    )
    namespace = slug(settings["namespace"], "provider.google_ads.namespace")
    currency = settings["currency"]
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigError("provider.google_ads.currency: expected an ISO 4217 currency code such as USD")
    account = settings.get("customer_id")
    login = settings.get("login_customer_id")
    limits = _limits(settings.get("limits"), "provider.google_ads.limits")

    document = mapping(document, "configuration", required={"campaigns"}, optional={"ad_templates"})
    raw_templates = document.get("ad_templates") or {}
    if not isinstance(raw_templates, dict):
        raise ConfigError("ad_templates: expected a mapping from template names to ads")
    templates = {
        slug(name, "ad_templates"): responsive_search_ad(ad, f"ad_templates.{name}")
        for name, ad in raw_templates.items()
    }
    campaigns: list[dict[str, Any]] = []
    for index, raw in enumerate(sequence(document["campaigns"], "campaigns", maximum=1000)):
        campaign = _campaign(raw, f"campaigns[{index}]", namespace, templates, limits)
        if any(c["id"] == campaign["id"] for c in campaigns):
            raise ConfigError(f"campaigns: campaign id {campaign['id']!r} is used more than once")
        campaigns.append(campaign)
    return {
        "namespace": namespace,
        "currency": currency,
        "customer_id": None if account is None else customer_id(account, "provider.google_ads.customer_id"),
        "login_customer_id": None if login is None else customer_id(login, "provider.google_ads.login_customer_id"),
        "campaigns": campaigns,
    }


def summary(desired: dict[str, Any]) -> str:
    campaigns = desired["campaigns"]
    groups = [g for c in campaigns for g in c["groups"]]
    keywords = sum(len(g["keywords"]) for g in groups)
    return f"{_plural(len(campaigns), 'campaign')}, {_plural(len(groups), 'ad group')}, {_plural(keywords, 'keyword')}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _limits(value: Any, path: str) -> dict[str, int]:
    limits = mapping({} if value is None else value, path, optional=set(DEFAULT_LIMITS))
    return {
        key: money_micros(limits.get(key, default), f"{path}.{key}", maximum_micros=_LIMIT_CEILING_MICROS)
        for key, default in DEFAULT_LIMITS.items()
    }


def _campaign(
    raw: Any, where: str, namespace: str, templates: dict[str, Any], limits: dict[str, int]
) -> dict[str, Any]:
    spec = mapping(
        raw,
        where,
        required={"id", "status", "daily_budget", "default_cpc", "locations", "ad_groups"},
        optional={"negative_keywords"},
    )
    cid = slug(spec["id"], f"{where}.id")
    where = f"campaigns.{cid}"
    status = spec["status"]
    if status not in ("PAUSED", "ENABLED"):
        raise ConfigError(f"{where}.status: expected PAUSED or ENABLED")
    budget = money_micros(spec["daily_budget"], f"{where}.daily_budget", maximum_micros=limits["daily_budget"])
    default_cpc = money_micros(spec["default_cpc"], f"{where}.default_cpc", maximum_micros=limits["cpc"])

    locations = [str(x) for x in sequence(spec["locations"], f"{where}.locations", minimum=1, maximum=100)]
    for location in locations:
        if not re.fullmatch(r"[1-9][0-9]{0,11}", location):
            raise ConfigError(
                f"{where}.locations: {location!r} is not a geo target constant ID (2840 is the United States)"
            )
    distinct(locations, f"{where}.locations")

    raw_negatives = sequence(spec.get("negative_keywords", []), f"{where}.negative_keywords", maximum=500)
    negatives = [keyword(k, f"{where}.negative_keywords") for k in raw_negatives]
    distinct(negatives, f"{where}.negative_keywords", key=lambda k: (k["text"], k["matchType"]))

    groups: list[dict[str, Any]] = []
    owner: dict[str, str] = {}
    for index, raw_group in enumerate(sequence(spec["ad_groups"], f"{where}.ad_groups", minimum=1, maximum=100)):
        group = _ad_group(raw_group, f"{where}.ad_groups[{index}]", where, templates, default_cpc, limits)
        if any(g["id"] == group["id"] for g in groups):
            raise ConfigError(f"{where}.ad_groups: ad group id {group['id']!r} is used more than once")
        for kw in group["keywords"]:
            if kw["text"] in owner:
                raise ConfigError(
                    f"{where}: keyword [{kw['text']}] is in ad groups {owner[kw['text']]} and {group['id']}; "
                    "a keyword may belong to one ad group per campaign"
                )
            owner[kw["text"]] = group["id"]
        groups.append(group)
    return {
        "id": cid,
        "name": campaign_name(namespace, cid),
        "status": status,
        "budget": budget,
        "locations": locations,
        "negatives": negatives,
        "groups": groups,
    }


def _ad_group(
    raw: Any,
    where: str,
    campaign_where: str,
    templates: dict[str, Any],
    default_cpc: int,
    limits: dict[str, int],
) -> dict[str, Any]:
    spec = mapping(raw, where, required={"id", "keywords"}, optional={"cpc", "ad", "ad_template"})
    gid = slug(spec["id"], f"{where}.id")
    where = f"{campaign_where}.ad_groups.{gid}"
    if ("ad" in spec) == ("ad_template" in spec):
        raise ConfigError(f"{where}: set exactly one of ad (an inline ad) or ad_template (a name from ad_templates)")
    if "ad_template" in spec:
        template = spec["ad_template"]
        if not isinstance(template, str) or template not in templates:
            known = ", ".join(sorted(templates)) or "none are defined"
            raise ConfigError(f"{where}.ad_template: unknown template {template!r} (known templates: {known})")
        ad = copy.deepcopy(templates[template])  # each ad group owns its ad; no shared objects
    else:
        template = None
        ad = responsive_search_ad(spec["ad"], f"{where}.ad")
    keywords = [
        keyword(k, f"{where}.keywords", exact_only=True)
        for k in sequence(spec["keywords"], f"{where}.keywords", minimum=1, maximum=500)
    ]
    distinct(keywords, f"{where}.keywords", key=lambda k: k["text"])
    bid = money_micros(spec["cpc"], f"{where}.cpc", maximum_micros=limits["cpc"]) if "cpc" in spec else default_cpc
    return {
        "id": gid,
        "name": ad_group_name(gid),
        "bid": bid,
        "keywords": keywords,
        "ad": ad,
        "ad_template": template,
    }
