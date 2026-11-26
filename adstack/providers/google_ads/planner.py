"""Pure, deterministic reconciliation of desired and live Google Ads state.

``plan`` makes no network requests. It compares the desired state from the
configuration with a snapshot of the account and returns:

- ``operations``: GoogleAdsService mutate operations, in dependency order;
- ``changes``: one record per affected resource, for people and tools to review;
- ``blockers``: conditions that make the plan unsafe to apply;
- ``requires``: the explicit permissions that applying the plan needs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from ...errors import PlanError
from .schema import PREFIX, keyword_label

NETWORK_SETTINGS = {
    "targetGoogleSearch": True,
    "targetSearchNetwork": False,
    "targetContentNetwork": False,
    "targetPartnerSearchNetwork": False,
}

_EMPTY = {"campaign": None, "budget": None, "groups": [], "keywords": [], "ads": [], "criteria": []}


def keyword_key(keyword: dict[str, Any]) -> tuple[str, Any]:
    return (" ".join(str(keyword.get("text", "")).lower().split()), keyword.get("matchType"))


def creative(ad: dict[str, Any]) -> tuple:
    """What makes two responsive search ads equivalent (order of assets does not matter)."""
    rsa = ad.get("responsiveSearchAd", {})

    def assets(field: str) -> list[tuple[str, str]]:
        return sorted((a.get("text", ""), a.get("pinnedField", "UNSPECIFIED")) for a in rsa.get(field, []))

    return (
        tuple(sorted(ad.get("finalUrls", []))),
        assets("headlines"),
        assets("descriptions"),
        rsa.get("path1", ""),
        rsa.get("path2", ""),
    )


def plan(
    desired: dict[str, Any],
    state: dict[str, Any],
    account: str,
    *,
    prune: bool = False,
    destroy: bool = False,
) -> dict[str, Any]:
    if state["currency"] != desired["currency"]:
        raise PlanError(
            f"The account's currency is {state['currency']}, but the configuration declares {desired['currency']}"
        )
    builder = _Builder(account, desired["currency"])
    if destroy:
        # Destroy never deletes anything in Google Ads: it pauses every managed campaign.
        for name, current in state["campaigns"].items():
            builder.pause_campaign(name, current, note="destroy")
        return builder.result()

    configured = {campaign["name"] for campaign in desired["campaigns"]}
    for spec in desired["campaigns"]:
        _reconcile(builder, spec, state["campaigns"].get(spec["name"]), prune)
    for name, current in state["campaigns"].items():
        if name in configured:
            continue
        status = current["campaign"].get("status")
        if prune:
            builder.pause_campaign(name, current, note="not in configuration")
        elif status != "PAUSED":
            builder.blockers.append(
                f"campaign {_short(name)}: exists in the account ({status}) but not in the configuration; "
                "re-plan with --prune to pause it"
            )
    return builder.result()


class _Builder:
    """Accumulates operations and change records for one plan."""

    def __init__(self, account: str, currency: str):
        self.base = f"customers/{account}"
        self.currency = currency
        self.operations: list[dict[str, Any]] = []
        self.changes: list[dict[str, Any]] = []
        self.blockers: list[str] = []
        self.enable = False
        self.increase = False
        self.live = False
        self.serving = False  # True while reconciling a campaign that is currently ENABLED
        self._temp = 0
        self._updates: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def money(self, micros: int | str) -> str:
        return f"{int(micros) / 1_000_000:.2f} {self.currency}"

    def temp_name(self, collection: str) -> str:
        """A temporary resource name: a negative ID, unique within the request."""
        self._temp -= 1
        return f"{self.base}/{collection}/{self._temp}"

    def operation(self, kind: str, body: dict[str, Any]) -> None:
        self.operations.append({f"{kind}Operation": body})
        if self.serving:
            self.live = True

    def record(self, action: str, resource: str, campaign: str, **labels: Any) -> dict[str, Any]:
        change = {
            "action": action,
            "resource": resource,
            "campaign": campaign,
            "ad_group": labels.pop("ad_group", None),
            "name": labels.pop("name", None),
        }
        change.update({key: value for key, value in labels.items() if value is not None})
        self.changes.append(change)
        return change

    def create(self, kind: str, fields: dict[str, Any], resource: str, campaign: str, **labels: Any) -> None:
        self.operation(kind, {"create": fields})
        self.record("create", resource, campaign, **labels)

    def update(
        self,
        kind: str,
        resource_name: str,
        fields: dict[str, Any],
        resource: str,
        campaign: str,
        *,
        diff: dict[str, list[Any]],
        mask: Iterable[str] | None = None,
        **labels: Any,
    ) -> None:
        mask = list(mask or fields)
        if resource_name in self._updates:
            # Google rejects a request that mutates one resource twice (ID_EXISTS_IN_MULTIPLE_MUTATES),
            # so later changes to an already-updated resource join its operation and update mask.
            body, change = self._updates[resource_name]
            if set(fields) & set(body["update"]):
                raise PlanError(f"internal error: conflicting updates to {resource_name}")
            body["update"].update(fields)
            body["updateMask"] += "," + ",".join(mask)
            change["diff"].update(diff)
            return
        body = {"update": {"resourceName": resource_name, **fields}, "updateMask": ",".join(mask)}
        self.operation(kind, body)
        change = self.record("update", resource, campaign, diff=dict(diff), **labels)
        self._updates[resource_name] = (body, change)

    def remove(self, kind: str, resource_name: str, resource: str, campaign: str, **labels: Any) -> None:
        self.operation(kind, {"remove": resource_name})
        self.record("remove", resource, campaign, **labels)

    def extra(
        self,
        prune: bool,
        kind: str,
        row: dict[str, Any],
        resource: str,
        campaign: str,
        *,
        name: str,
        ad_group: str | None = None,
        remove: bool = False,
    ) -> None:
        """Handle something that exists in the account but not in the configuration."""
        if not prune:
            where = f"campaign {campaign}" + (f", ad group {ad_group}" if ad_group else "")
            verb = "remove" if remove else "pause"
            self.blockers.append(
                f"{where}: {resource.replace('_', ' ')} {name} exists in the account but not in the "
                f"configuration; re-plan with --prune to {verb} it"
            )
        elif remove:
            self.remove(
                kind, row["resourceName"], resource, campaign, ad_group=ad_group, name=name, note="not in configuration"
            )
        elif row.get("status") != "PAUSED":
            self.update(
                kind,
                row["resourceName"],
                {"status": "PAUSED"},
                resource,
                campaign,
                ad_group=ad_group,
                name=name,
                note="not in configuration",
                diff={"status": [row.get("status"), "PAUSED"]},
            )

    def pause_campaign(self, name: str, current: dict[str, Any], *, note: str) -> None:
        campaign = current["campaign"]
        status = campaign.get("status")
        if status == "PAUSED":
            return
        self.serving = status == "ENABLED"
        self.update(
            "campaign",
            campaign["resourceName"],
            {"status": "PAUSED"},
            "campaign",
            _short(name),
            name=name,
            note=note,
            diff={"status": [status, "PAUSED"]},
        )
        self.serving = False

    def result(self) -> dict[str, Any]:
        return {
            "changes": self.changes,
            "blockers": self.blockers,
            "requires": {
                "allow_enable": self.enable,
                "allow_budget_increase": self.increase,
                "allow_live_changes": self.live,
            },
            "operations": self.operations,
        }


def _short(name: str) -> str:
    """``adstack:<namespace>:<id>`` -> ``<id>``."""
    return name.split(":", 2)[-1]


def _snake(field: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", field).lower()


def _unique(rows: Iterable[dict[str, Any]], key: Callable[[dict[str, Any]], Any], what: str) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for row in rows:
        identity = key(row)
        if identity in result:
            raise PlanError(f"duplicate {what} in the account: {identity}")
        result[identity] = row
    return result


def _reconcile(b: _Builder, spec: dict[str, Any], current: dict[str, Any] | None, prune: bool) -> None:
    current = current or _EMPTY
    if current["campaign"] is None:
        campaign_rn = _create_campaign(b, spec)
    else:
        campaign_rn = _reconcile_settings(b, spec, current)
    _reconcile_criteria(b, spec, current, campaign_rn, prune)
    _reconcile_ad_groups(b, spec, current, campaign_rn, prune)
    b.serving = False


def _create_campaign(b: _Builder, spec: dict[str, Any]) -> str:
    cid = spec["id"]
    if spec["status"] != "PAUSED":
        raise PlanError(
            f"campaign {cid}: new campaigns are created PAUSED. Create it with status: PAUSED, "
            "then set status: ENABLED in a later plan"
        )
    b.serving = False
    budget_rn, campaign_rn = b.temp_name("campaignBudgets"), b.temp_name("campaigns")
    b.record(
        "create",
        "campaign",
        cid,
        name=spec["name"],
        attributes={
            "status": "PAUSED",
            "type": "SEARCH",
            "bidding": "MANUAL_CPC",
            "networks": "Google Search only",
            "geo_targeting": "PRESENCE",
        },
    )
    b.record("create", "budget", cid, name="daily budget", attributes={"amount": b.money(spec["budget"])})
    b.operation(
        "campaignBudget",
        {
            "create": {
                "resourceName": budget_rn,
                "name": f"{spec['name']}:budget",
                "amountMicros": str(spec["budget"]),
                "deliveryMethod": "STANDARD",
                "explicitlyShared": False,
            }
        },
    )
    b.operation(
        "campaign",
        {
            "create": {
                "resourceName": campaign_rn,
                "name": spec["name"],
                "status": "PAUSED",
                "advertisingChannelType": "SEARCH",
                "campaignBudget": budget_rn,
                "manualCpc": {},
                "networkSettings": dict(NETWORK_SETTINGS),
                "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
            }
        },
    )
    return campaign_rn


def _reconcile_settings(b: _Builder, spec: dict[str, Any], current: dict[str, Any]) -> str:
    cid, campaign, budget = spec["id"], current["campaign"], current["budget"]
    if campaign.get("advertisingChannelType") != "SEARCH" or campaign.get("biddingStrategyType") != "MANUAL_CPC":
        raise PlanError(
            f"campaign {cid}: {spec['name']} exists but is not a SEARCH campaign with MANUAL_CPC bidding; "
            "Adstack will not adopt or convert it"
        )
    status = campaign.get("status")
    if status not in ("PAUSED", "ENABLED"):
        raise PlanError(f"campaign {cid}: unexpected status {status!r}")
    if (
        not budget
        or budget.get("explicitlyShared")
        or int(budget.get("referenceCount", 0)) > 1
        or budget.get("period") != "DAILY"
    ):
        raise PlanError(f"campaign {cid}: its budget must be a dedicated daily budget that no other campaign uses")
    b.serving = status == "ENABLED"
    campaign_rn = campaign["resourceName"]
    labels = {"name": spec["name"]}
    network = campaign.get("networkSettings", {})
    drift = {key: value for key, value in NETWORK_SETTINGS.items() if network.get(key, False) != value}
    if drift:
        b.update(
            "campaign",
            campaign_rn,
            {"networkSettings": drift},
            "campaign",
            cid,
            **labels,
            diff={_snake(key): [network.get(key, False), value] for key, value in drift.items()},
            mask=[f"networkSettings.{key}" for key in drift],
        )
    geo = campaign.get("geoTargetTypeSetting", {}).get("positiveGeoTargetType")
    if geo != "PRESENCE":
        b.update(
            "campaign",
            campaign_rn,
            {"geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"}},
            "campaign",
            cid,
            **labels,
            diff={"positive_geo_target_type": [geo, "PRESENCE"]},
            mask=["geoTargetTypeSetting.positiveGeoTargetType"],
        )
    if status != spec["status"]:
        b.enable = b.enable or spec["status"] == "ENABLED"
        b.update(
            "campaign",
            campaign_rn,
            {"status": spec["status"]},
            "campaign",
            cid,
            **labels,
            diff={"status": [status, spec["status"]]},
        )
    amount = int(budget["amountMicros"])
    if amount != spec["budget"]:
        b.increase = b.increase or spec["budget"] > amount
        b.update(
            "campaignBudget",
            budget["resourceName"],
            {"amountMicros": str(spec["budget"])},
            "budget",
            cid,
            name="daily budget",
            diff={"amount": [b.money(amount), b.money(spec["budget"])]},
        )
    return campaign_rn


def _reconcile_criteria(
    b: _Builder, spec: dict[str, Any], current: dict[str, Any], campaign_rn: str, prune: bool
) -> None:
    cid = spec["id"]
    locations: dict[str, dict[str, Any]] = {}
    negatives: dict[tuple[str, Any], dict[str, Any]] = {}
    for criterion in current["criteria"]:
        kind, negative = criterion.get("type"), bool(criterion.get("negative", False))
        if kind == "LOCATION" and not negative:
            geo = criterion["location"]["geoTargetConstant"].rsplit("/", 1)[-1]
            if geo in locations:
                raise PlanError(f"campaign {cid}: location {geo} is targeted twice in the account")
            locations[geo] = criterion
        elif kind == "KEYWORD" and negative:
            key = keyword_key(criterion["keyword"])
            if key in negatives:
                raise PlanError(f"campaign {cid}: negative keyword {keyword_label(criterion['keyword'])} is duplicated")
            negatives[key] = criterion
        else:
            what = f"{'negative ' if negative else ''}{kind}"
            b.blockers.append(
                f"campaign {cid}: unmanaged {what} criterion {criterion['resourceName']}; remove or review it manually"
            )
    for geo in spec["locations"]:
        if geo not in locations:
            b.create(
                "campaignCriterion",
                {"campaign": campaign_rn, "location": {"geoTargetConstant": f"geoTargetConstants/{geo}"}},
                "location",
                cid,
                name=geo,
            )
        elif locations[geo].get("status") == "PAUSED":
            b.update(
                "campaignCriterion",
                locations[geo]["resourceName"],
                {"status": "ENABLED"},
                "location",
                cid,
                name=geo,
                diff={"status": ["PAUSED", "ENABLED"]},
            )
    for geo, criterion in locations.items():
        if geo not in spec["locations"]:
            b.extra(prune, "campaignCriterion", criterion, "location", cid, name=geo, remove=True)

    wanted = {keyword_key(k): k for k in spec["negatives"]}
    for key, negative_keyword in wanted.items():
        label = keyword_label(negative_keyword)
        if key not in negatives:
            b.create(
                "campaignCriterion",
                {"campaign": campaign_rn, "negative": True, "keyword": negative_keyword},
                "negative_keyword",
                cid,
                name=label,
            )
        elif negatives[key].get("status") == "PAUSED":
            b.blockers.append(f"campaign {cid}: negative keyword {label} is paused in the account; review it manually")
    for key, criterion in negatives.items():
        if key not in wanted:
            b.extra(
                prune,
                "campaignCriterion",
                criterion,
                "negative_keyword",
                cid,
                name=keyword_label(criterion["keyword"]),
                remove=True,
            )


def _reconcile_ad_groups(
    b: _Builder, spec: dict[str, Any], current: dict[str, Any], campaign_rn: str, prune: bool
) -> None:
    cid = spec["id"]
    existing_groups = _unique(current["groups"], lambda g: g["name"], f"ad group name in campaign {cid}")
    keywords: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ads: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current["keywords"]:
        keywords[row["adGroup"]].append(row)
    for row in current["ads"]:
        ads[row["adGroup"]].append(row)

    for group in spec["groups"]:
        gid = group["id"]
        labels = {"ad_group": gid}
        old = existing_groups.get(group["name"])
        if old is None:
            group_rn = b.temp_name("adGroups")
            b.create(
                "adGroup",
                {
                    "resourceName": group_rn,
                    "campaign": campaign_rn,
                    "name": group["name"],
                    "type": "SEARCH_STANDARD",
                    "status": "ENABLED",
                    "cpcBidMicros": str(group["bid"]),
                },
                "ad_group",
                cid,
                **labels,
                name=group["name"],
                attributes={"cpc_bid": b.money(group["bid"])},
            )
        else:
            group_rn = old["resourceName"]
            if old.get("type") != "SEARCH_STANDARD":
                raise PlanError(f"campaign {cid}, ad group {gid}: the existing ad group has an unexpected type")
            old_bid = int(old.get("cpcBidMicros", 0))
            if old_bid != group["bid"]:
                b.increase = b.increase or group["bid"] > old_bid
                b.update(
                    "adGroup",
                    group_rn,
                    {"cpcBidMicros": str(group["bid"])},
                    "ad_group",
                    cid,
                    **labels,
                    name=group["name"],
                    diff={"cpc_bid": [b.money(old_bid), b.money(group["bid"])]},
                )
            if old.get("status") != "ENABLED":
                b.update(
                    "adGroup",
                    group_rn,
                    {"status": "ENABLED"},
                    "ad_group",
                    cid,
                    **labels,
                    name=group["name"],
                    diff={"status": [old.get("status"), "ENABLED"]},
                )
        _reconcile_keywords(b, cid, group, group_rn, keywords[group_rn], prune)
        _reconcile_ad(b, cid, group, group_rn, ads[group_rn])

    configured = {group["name"] for group in spec["groups"]}
    for name, old in existing_groups.items():
        if name in configured or old.get("status") == "PAUSED":
            continue
        if name.startswith(f"{PREFIX}:"):
            b.extra(prune, "adGroup", old, "ad_group", cid, ad_group=name.split(":", 1)[1], name=name)
        else:
            b.blockers.append(f"campaign {cid}: unmanaged active ad group {name!r}; pause or review it manually")


def _reconcile_keywords(
    b: _Builder, cid: str, group: dict[str, Any], group_rn: str, rows: list[dict[str, Any]], prune: bool
) -> None:
    gid = group["id"]
    for row in rows:
        if row.get("negative"):
            b.blockers.append(
                f"campaign {cid}, ad group {gid}: unmanaged negative keyword {keyword_label(row['keyword'])}; "
                "review it manually"
            )
    existing = _unique(
        (row for row in rows if not row.get("negative")),
        lambda row: keyword_key(row["keyword"]),
        f"keyword in campaign {cid}, ad group {gid}",
    )
    wanted = {keyword_key(k): k for k in group["keywords"]}
    for key, wanted_keyword in wanted.items():
        label = keyword_label(wanted_keyword)
        if key not in existing:
            b.create(
                "adGroupCriterion",
                {"adGroup": group_rn, "status": "ENABLED", "keyword": wanted_keyword},
                "keyword",
                cid,
                ad_group=gid,
                name=label,
            )
        elif existing[key].get("status") != "ENABLED":
            b.update(
                "adGroupCriterion",
                existing[key]["resourceName"],
                {"status": "ENABLED"},
                "keyword",
                cid,
                ad_group=gid,
                name=label,
                diff={"status": [existing[key].get("status"), "ENABLED"]},
            )
    for key, row in existing.items():
        if key not in wanted and row.get("status") != "PAUSED":
            b.extra(prune, "adGroupCriterion", row, "keyword", cid, ad_group=gid, name=keyword_label(row["keyword"]))


def _reconcile_ad(b: _Builder, cid: str, group: dict[str, Any], group_rn: str, rows: list[dict[str, Any]]) -> None:
    """Keep exactly one enabled responsive search ad that matches the configuration."""
    gid = group["id"]
    responsive = [row for row in rows if row["ad"].get("type") == "RESPONSIVE_SEARCH_AD"]
    for row in rows:
        if row["ad"].get("type") != "RESPONSIVE_SEARCH_AD" and row.get("status") != "PAUSED":
            b.blockers.append(
                f"campaign {cid}, ad group {gid}: unmanaged active {row['ad'].get('type')} ad "
                f"{row['resourceName']}; pause or review it manually"
            )
    target = creative(group["ad"])
    matches = [row for row in responsive if creative(row["ad"]) == target]
    chosen = next((row for row in matches if row.get("status") == "ENABLED"), matches[0] if matches else None)
    if chosen is None:
        rsa = group["ad"]["responsiveSearchAd"]
        b.create(
            "adGroupAd",
            {"adGroup": group_rn, "status": "ENABLED", "ad": group["ad"]},
            "ad",
            cid,
            ad_group=gid,
            name="responsive search ad",
            attributes={
                "template": group["ad_template"] or "(inline)",
                "headlines": len(rsa["headlines"]),
                "descriptions": len(rsa["descriptions"]),
                "final_url": group["ad"]["finalUrls"][0],
            },
        )
    elif chosen.get("status") != "ENABLED":
        b.update(
            "adGroupAd",
            chosen["resourceName"],
            {"status": "ENABLED"},
            "ad",
            cid,
            ad_group=gid,
            name=_ad_name(chosen),
            diff={"status": [chosen.get("status"), "ENABLED"]},
        )
    for row in responsive:
        if row is not chosen and row.get("status") != "PAUSED":
            b.update(
                "adGroupAd",
                row["resourceName"],
                {"status": "PAUSED"},
                "ad",
                cid,
                ad_group=gid,
                name=_ad_name(row),
                note="superseded",
                diff={"status": [row.get("status"), "PAUSED"]},
            )


def _ad_name(row: dict[str, Any]) -> str:
    return f"responsive search ad {row['resourceName'].rsplit('~', 1)[-1]}"


def inventory(state: dict[str, Any], currency: str) -> list[dict[str, Any]]:
    """Flatten a snapshot into display records: what exists in the account under the namespace."""

    def money(micros: Any) -> str:
        return f"{int(micros) / 1_000_000:.2f} {currency}"

    records: list[dict[str, Any]] = []
    for name, entry in state["campaigns"].items():
        cid, campaign = _short(name), entry["campaign"]
        records.append(
            {
                "resource": "campaign",
                "campaign": cid,
                "ad_group": None,
                "name": name,
                "status": campaign.get("status"),
                "details": f"daily budget {money(entry['budget']['amountMicros'])}",
            }
        )
        for criterion in entry["criteria"]:
            if criterion.get("type") == "LOCATION":
                resource = "excluded_location" if criterion.get("negative") else "location"
                label = criterion["location"]["geoTargetConstant"].rsplit("/", 1)[-1]
            else:
                resource = "negative_keyword" if criterion.get("negative") else "keyword"
                label = keyword_label(criterion.get("keyword", {}))
            records.append(
                {
                    "resource": resource,
                    "campaign": cid,
                    "ad_group": None,
                    "name": label,
                    "status": criterion.get("status"),
                }
            )
        for group in entry["groups"]:
            managed = group["name"].startswith(f"{PREFIX}:")
            gid = group["name"].split(":", 1)[1] if managed else group["name"]
            records.append(
                {
                    "resource": "ad_group",
                    "campaign": cid,
                    "ad_group": gid,
                    "name": group["name"],
                    "status": group.get("status"),
                    "details": f"cpc_bid {money(group.get('cpcBidMicros', 0))}"
                    + ("" if managed else " (not managed by Adstack)"),
                }
            )
            for row in entry["keywords"]:
                if row["adGroup"] == group["resourceName"]:
                    label = keyword_label(row["keyword"])
                    records.append(
                        {
                            "resource": "negative_keyword" if row.get("negative") else "keyword",
                            "campaign": cid,
                            "ad_group": gid,
                            "name": label,
                            "status": row.get("status"),
                        }
                    )
            for row in entry["ads"]:
                if row["adGroup"] == group["resourceName"]:
                    kind = row["ad"].get("type", "AD").lower().replace("_", " ")
                    records.append(
                        {
                            "resource": "ad",
                            "campaign": cid,
                            "ad_group": gid,
                            "name": f"{kind} {row['resourceName'].rsplit('~', 1)[-1]}",
                            "status": row.get("status"),
                        }
                    )
    return records
