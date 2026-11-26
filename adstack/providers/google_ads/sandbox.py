"""An offline stand-in for a Google Ads account.

The sandbox keeps resources in a local JSON file and applies the same mutate
operations Adstack sends to Google, so configurations, plans and the MCP
server can be tried without an ad account or credentials (``--sandbox``).

It enforces the parts of Google's mutate contract that Adstack relies on:
requests are atomic; temporary resource names must be created before they are
referenced; referenced resources must exist; and one request may not mutate
the same resource twice. It does not emulate ad policy review, keyword
validation, auctions or billing, and its reports contain zero metrics.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ...errors import ConfigError, ProviderError
from .api import REPORT_DAYS, REPORT_KINDS
from .schema import PREFIX

SANDBOX_FORMAT = "adstack-sandbox/1"
SANDBOX_ACCOUNT = "0000000000"

COLLECTIONS = {
    "campaignBudgetOperation": "campaignBudgets",
    "campaignOperation": "campaigns",
    "campaignCriterionOperation": "campaignCriteria",
    "adGroupOperation": "adGroups",
    "adGroupCriterionOperation": "adGroupCriteria",
    "adGroupAdOperation": "adGroupAds",
}

# Fields returned by the snapshot, matching the Google Ads API backend's GAQL selections.
_CAMPAIGN = (
    "resourceName",
    "name",
    "status",
    "advertisingChannelType",
    "campaignBudget",
    "biddingStrategyType",
    "networkSettings",
    "geoTargetTypeSetting",
)
_BUDGET = ("resourceName", "amountMicros", "explicitlyShared", "period")
_GROUP = ("resourceName", "name", "status", "type", "cpcBidMicros")
_KEYWORD = ("resourceName", "adGroup", "status", "keyword", "negative")
_AD = ("resourceName", "adGroup", "status", "ad")
_CRITERION = ("resourceName", "status", "type", "negative", "location", "keyword")


def _pick(resource: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: copy.deepcopy(resource[field]) for field in fields if field in resource}


def _resolve(value: Any, temporary: dict[str, str]) -> Any:
    """Replace references to temporary resource names created earlier in the request."""
    if isinstance(value, str):
        return temporary.get(value, value)
    if isinstance(value, list):
        return [_resolve(item, temporary) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, temporary) for key, item in value.items()}
    return value


class SandboxBackend:
    """A fake Google Ads account stored in a JSON file."""

    sandbox = True
    account = SANDBOX_ACCOUNT

    def __init__(self, path: str | Path, namespace: str, currency: str):
        self.path = Path(path)
        self.namespace = namespace
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProviderError(f"{self.path}: cannot read the sandbox file: {exc}") from None
            if not isinstance(self.data, dict) or self.data.get("format") != SANDBOX_FORMAT:
                raise ProviderError(f"{self.path}: not an Adstack sandbox file")
        else:
            self.data = {
                "format": SANDBOX_FORMAT,
                "account": SANDBOX_ACCOUNT,
                "currency": currency,
                "next_id": 1001,
                "resources": {collection: {} for collection in COLLECTIONS.values()},
            }

    # -- Backend interface -------------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        resources = self.data["resources"]
        prefix = f"{PREFIX}:{self.namespace}:"
        live = {rn: c for rn, c in resources["campaigns"].items() if c["status"] != "REMOVED"}
        references = Counter(c["campaignBudget"] for c in live.values())
        state: dict[str, Any] = {"currency": self.data["currency"], "campaigns": {}}
        for campaign_rn, campaign in sorted(live.items()):
            if not campaign["name"].startswith(prefix):
                continue
            budget = resources["campaignBudgets"][campaign["campaignBudget"]]
            groups = {
                rn: group
                for rn, group in sorted(resources["adGroups"].items())
                if group["campaign"] == campaign_rn and group["status"] != "REMOVED"
            }
            state["campaigns"][campaign["name"]] = {
                "campaign": _pick(campaign, _CAMPAIGN),
                "budget": {**_pick(budget, _BUDGET), "referenceCount": str(references[campaign["campaignBudget"]])},
                "groups": [_pick(group, _GROUP) for group in groups.values()],
                "keywords": [
                    _pick(row, _KEYWORD)
                    for _, row in sorted(resources["adGroupCriteria"].items())
                    if row["adGroup"] in groups
                ],
                "ads": [
                    _pick(row, _AD)
                    for _, row in sorted(resources["adGroupAds"].items())
                    if row["adGroup"] in groups and row["status"] != "REMOVED"
                ],
                "criteria": [
                    _pick(row, _CRITERION)
                    for _, row in sorted(resources["campaignCriteria"].items())
                    if row["campaign"] == campaign_rn
                ],
            }
        state["campaigns"] = dict(sorted(state["campaigns"].items()))
        return state

    def mutate(self, operations: list[dict[str, Any]], *, validate_only: bool) -> dict[str, Any]:
        data = copy.deepcopy(self.data)
        temporary: dict[str, str] = {}
        touched: set[str] = set()
        responses = []
        for index, operation in enumerate(operations):
            try:
                resource_name = self._apply(data, operation, temporary)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ProviderError(f"sandbox rejected operation {index}: malformed operation ({exc!r})") from None
            except ProviderError as exc:
                raise ProviderError(f"sandbox rejected operation {index}: {exc}") from None
            if resource_name in touched:
                raise ProviderError(
                    f"sandbox rejected operation {index}: ID_EXISTS_IN_MULTIPLE_MUTATES: cannot mutate the same "
                    f"resource twice in one request ({resource_name})"
                )
            touched.add(resource_name)
            kind = next(iter(operation)).replace("Operation", "Result")
            responses.append({kind: {"resourceName": resource_name}})
        if not validate_only:
            self.data = data
            self.save()
        return {"mutateOperationResponses": responses}

    def report(self, kind: str, days: int, *, all_campaigns: bool = False) -> list[dict[str, Any]]:
        if kind not in REPORT_KINDS:
            raise ConfigError(f"unknown report {kind!r} (choose from {', '.join(REPORT_KINDS)})")
        if days not in REPORT_DAYS:
            raise ConfigError("days must be 7, 14 or 30")
        if kind == "search_terms":
            return []
        prefix = f"{PREFIX}:{self.namespace}:"
        campaigns = sorted(self.data["resources"]["campaigns"].values(), key=lambda c: c["name"])
        return [
            {
                "campaign": c["name"],
                "status": c["status"],
                "impressions": 0,
                "clicks": 0,
                "cost": "0.00",
                "conversions": 0.0,
            }
            for c in campaigns
            if c["status"] != "REMOVED" and (all_campaigns or c["name"].startswith(prefix))
        ]

    # -- Storage -----------------------------------------------------------------------------------

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise ProviderError(f"cannot write the sandbox file {self.path}: {exc}") from None

    # -- Mutate emulation --------------------------------------------------------------------------

    def _apply(self, data: dict[str, Any], operation: dict[str, Any], temporary: dict[str, str]) -> str:
        if not isinstance(operation, dict) or len(operation) != 1:
            raise ProviderError("each operation must contain exactly one resource operation")
        ((kind, body),) = operation.items()
        collection = COLLECTIONS.get(kind)
        if collection is None:
            raise ProviderError(f"unsupported operation {kind!r}")
        if "create" in body:
            return self._create(data, collection, body["create"], temporary)
        if "update" in body:
            return self._update(data, collection, body["update"], body.get("updateMask", ""))
        if "remove" in body:
            return self._remove(data, collection, body["remove"])
        raise ProviderError("operation has no create, update or remove")

    @staticmethod
    def _existing(data: dict[str, Any], collection: str, resource_name: Any) -> str:
        resource = data["resources"][collection].get(resource_name) if isinstance(resource_name, str) else None
        if resource is None or resource.get("status") == "REMOVED":
            raise ProviderError(f"RESOURCE_NOT_FOUND: {resource_name!r}")
        return resource_name

    def _create(self, data: dict[str, Any], collection: str, fields: dict[str, Any], temporary: dict[str, str]) -> str:
        fields = _resolve(copy.deepcopy(fields), temporary)
        temp_name = fields.pop("resourceName", None)
        if temp_name is not None:
            if not re.fullmatch(rf"customers/{self.account}/{collection}/-[0-9]+", str(temp_name)):
                raise ProviderError(f"invalid temporary resource name {temp_name!r}")
            if temp_name in temporary:
                raise ProviderError(f"temporary resource name {temp_name!r} is used twice")
        resources = data["resources"]
        new_id = data["next_id"]
        data["next_id"] += 1
        base = f"customers/{self.account}"

        if collection == "campaignBudgets":
            resource_name = f"{base}/campaignBudgets/{new_id}"
            resource = {
                "name": fields["name"],
                "amountMicros": str(int(fields["amountMicros"])),
                "explicitlyShared": bool(fields.get("explicitlyShared", False)),
                "deliveryMethod": fields.get("deliveryMethod", "STANDARD"),
                "period": "DAILY",
            }
        elif collection == "campaigns":
            budget = self._existing(data, "campaignBudgets", fields.get("campaignBudget"))
            if any(c["name"] == fields["name"] and c["status"] != "REMOVED" for c in resources["campaigns"].values()):
                raise ProviderError(f"DUPLICATE_CAMPAIGN_NAME: {fields['name']!r}")
            if fields.get("status") not in ("ENABLED", "PAUSED"):
                raise ProviderError(f"invalid campaign status {fields.get('status')!r}")
            resource_name = f"{base}/campaigns/{new_id}"
            resource = {
                "name": fields["name"],
                "status": fields["status"],
                "advertisingChannelType": fields["advertisingChannelType"],
                "campaignBudget": budget,
                "biddingStrategyType": "MANUAL_CPC" if "manualCpc" in fields else "UNSPECIFIED",
                "networkSettings": fields.get("networkSettings", {}),
                "geoTargetTypeSetting": fields.get("geoTargetTypeSetting", {}),
            }
        elif collection == "campaignCriteria":
            campaign = self._existing(data, "campaigns", fields.get("campaign"))
            kind = "LOCATION" if "location" in fields else "KEYWORD" if "keyword" in fields else None
            if kind is None:
                raise ProviderError("campaign criterion needs a location or a keyword")
            resource_name = f"{base}/campaignCriteria/{campaign.rsplit('/', 1)[1]}~{new_id}"
            resource = {
                "campaign": campaign,
                "status": fields.get("status", "ENABLED"),
                "type": kind,
                "negative": bool(fields.get("negative", False)),
                kind.lower(): fields[kind.lower()],
            }
        elif collection == "adGroups":
            campaign = self._existing(data, "campaigns", fields.get("campaign"))
            if any(
                g["campaign"] == campaign and g["name"] == fields["name"] and g["status"] != "REMOVED"
                for g in resources["adGroups"].values()
            ):
                raise ProviderError(f"DUPLICATE_ADGROUP_NAME: {fields['name']!r}")
            resource_name = f"{base}/adGroups/{new_id}"
            resource = {
                "campaign": campaign,
                "name": fields["name"],
                "status": fields.get("status", "ENABLED"),
                "type": fields.get("type", "SEARCH_STANDARD"),
                "cpcBidMicros": str(int(fields.get("cpcBidMicros", 0))),
            }
        elif collection == "adGroupCriteria":
            group = self._existing(data, "adGroups", fields.get("adGroup"))
            keyword = fields["keyword"]
            negative = bool(fields.get("negative", False))
            if any(
                k["adGroup"] == group and k["keyword"] == keyword and k["negative"] == negative
                for k in resources["adGroupCriteria"].values()
            ):
                raise ProviderError(f"duplicate keyword {keyword!r}")
            resource_name = f"{base}/adGroupCriteria/{group.rsplit('/', 1)[1]}~{new_id}"
            resource = {
                "adGroup": group,
                "status": fields.get("status", "ENABLED"),
                "keyword": keyword,
                "negative": negative,
            }
        else:  # adGroupAds
            group = self._existing(data, "adGroups", fields.get("adGroup"))
            ad = dict(fields["ad"])
            if "responsiveSearchAd" in ad:
                ad["type"] = "RESPONSIVE_SEARCH_AD"
            resource_name = f"{base}/adGroupAds/{group.rsplit('/', 1)[1]}~{new_id}"
            resource = {"adGroup": group, "status": fields.get("status", "ENABLED"), "ad": ad}

        resource["resourceName"] = resource_name
        resources[collection][resource_name] = resource
        if temp_name is not None:
            temporary[temp_name] = resource_name
        return resource_name

    def _update(self, data: dict[str, Any], collection: str, fields: dict[str, Any], mask: str) -> str:
        fields = dict(fields)
        resource_name = self._existing(data, collection, fields.pop("resourceName", None))
        resource = data["resources"][collection][resource_name]
        paths = [path for path in str(mask).split(",") if path]
        if not paths:
            raise ProviderError("update operations need an updateMask")
        for path in paths:
            parts = path.split(".")
            value: Any = fields
            for part in parts:
                if not isinstance(value, dict) or part not in value:
                    raise ProviderError(f"updateMask path {path!r} is missing from the update")
                value = value[part]
            target = resource
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = copy.deepcopy(value)
        return resource_name

    def _remove(self, data: dict[str, Any], collection: str, resource_name: Any) -> str:
        resource_name = self._existing(data, collection, resource_name)
        store = data["resources"][collection]
        if collection in ("campaignCriteria", "adGroupCriteria"):
            del store[resource_name]
        else:
            store[resource_name]["status"] = "REMOVED"
        return resource_name
