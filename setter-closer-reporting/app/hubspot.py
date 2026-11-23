"""HubSpot booking metrics.

HubSpot has no public API that returns the numbers shown on a custom dashboard, so this module
re-runs the two saved reports behind the "Setters Matrix" dashboard through the official CRM APIs:

* "Meeting Booking - Daily" (report 155571610)
    distinct CONTACTS associated with deals whose "Date entered Counselling Scheduled
    (Direct Enrolment)" (hs_v2_date_entered_1048885393) is today in IST,
    excluding contacts in lists 1724 and 6614, grouped by the deal's Setter (asst_counsellor).
* "CCs - today" (report 163016740)
    distinct DEALS whose "Date entered Counselling Completed (Direct Enrolment)"
    (hs_v2_date_entered_1133451100) is today in IST, grouped by Setter (asst_counsellor).

"Today" runs from 00:00 IST to the report's cutoff (2/5/7/9 PM), so each snapshot shows the
position at its own reporting time. Every count keeps the deal and contact IDs behind it.

Required private-app scopes: crm.objects.deals.read, crm.objects.contacts.read,
crm.schemas.deals.read, crm.objects.owners.read, crm.lists.read
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import settings

UNASSIGNED = "__unassigned__"


class HubSpotError(Exception):
    pass


@dataclass
class BookingResult:
    rows: list[dict] = field(default_factory=list)  # one per setter value
    total_bookings: int = 0
    total_cc: int = 0
    meta: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class HubSpotClient:
    def __init__(self, token: str | None = None, base_url: str | None = None,
                 transport: httpx.BaseTransport | None = None, max_attempts: int = 4,
                 backoff_seconds: float = 2.0):
        token = token or settings.hubspot_token
        if not token:
            raise HubSpotError("HubSpot isn't connected: HUBSPOT_TOKEN is not set.")
        self.client = httpx.Client(
            base_url=base_url or settings.hubspot_base_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30.0, transport=transport,
        )
        self.max_attempts = max_attempts
        self.backoff = backoff_seconds
        self.request_count = 0

    def close(self):
        self.client.close()

    def request(self, method: str, url: str, **kwargs) -> dict:
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self.request_count += 1
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc.__class__.__name__}"
            else:
                if resp.status_code < 300:
                    return resp.json() if resp.content else {}
                if resp.status_code in (401, 403):
                    raise HubSpotError(
                        f"HubSpot refused access ({resp.status_code}) on {url}. Check the token and that the "
                        f"private app has the required scopes. {_brief(resp)}")
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code} {_brief(resp)}"
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and attempt < self.max_attempts:
                        time.sleep(min(float(retry_after), 30))
                        continue
                else:
                    raise HubSpotError(f"HubSpot returned {resp.status_code} on {url}: {_brief(resp)}")
            if attempt < self.max_attempts:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
        raise HubSpotError(f"HubSpot request failed after {self.max_attempts} attempts on {url}: {last_error}")

    # ---- primitives -------------------------------------------------------------------------
    def search_deals_entered_stage(self, stage_id: str, start: datetime, end: datetime) -> tuple[list[dict], str]:
        """Deals whose date-entered-stage falls in [start, end). Returns (deals, property_used)."""
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        last_exc = None
        for prop in (f"hs_v2_date_entered_{stage_id}", f"hs_date_entered_{stage_id}"):
            try:
                return self._search_all("deals", [
                    {"propertyName": prop, "operator": "GTE", "value": str(start_ms)},
                    {"propertyName": prop, "operator": "LT", "value": str(end_ms)},
                ], [settings.setter_property, "dealname", prop]), prop
            except HubSpotError as exc:
                if "400" not in str(exc):
                    raise
                last_exc = exc
        raise last_exc  # type: ignore[misc]

    def _search_all(self, obj: str, filters: list[dict], properties: list[str]) -> list[dict]:
        out, after = [], None
        while True:
            body = {"filterGroups": [{"filters": filters}], "properties": properties, "limit": 200,
                    "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
            if after:
                body["after"] = after
            data = self.request("POST", f"/crm/v3/objects/{obj}/search", json=body)
            out.extend(data.get("results", []))
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return out
            if len(out) >= 10000:
                raise HubSpotError("More than 10,000 deals matched; HubSpot search can't page past that.")

    def deal_contacts(self, deal_ids: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {d: [] for d in deal_ids}
        for i in range(0, len(deal_ids), 1000):
            batch = deal_ids[i:i + 1000]
            data = self.request("POST", "/crm/v4/associations/deals/contacts/batch/read",
                                json={"inputs": [{"id": d} for d in batch]})
            for item in data.get("results", []):
                from_id = str(item["from"]["id"])
                result[from_id] = sorted({str(t["toObjectId"]) for t in item.get("to", [])})
        return result

    def contact_list_ids(self, contact_id: str) -> set[str]:
        data = self.request("GET", f"/crm/v3/lists/records/0-1/{contact_id}/memberships")
        return {str(r.get("listId")) for r in data.get("results", [])}

    def setter_labels(self) -> dict[str, str]:
        """Option value -> display label of the Setter property (this is the name HubSpot shows)."""
        data = self.request("GET", f"/crm/v3/properties/deals/{settings.setter_property}")
        return {str(o["value"]): o.get("label") or "" for o in data.get("options", [])}

    def owners(self) -> list[dict]:
        out, after = [], None
        for archived in ("false", "true"):
            after = None
            while True:
                params = {"limit": 500, "archived": archived}
                if after:
                    params["after"] = after
                data = self.request("GET", "/crm/v3/owners", params=params)
                out.extend(data.get("results", []))
                after = (data.get("paging") or {}).get("next", {}).get("after")
                if not after:
                    break
        return out

    def test_connection(self) -> dict:
        labels = self.setter_labels()
        return {"setter_property": settings.setter_property, "setter_options": len(labels)}


def _brief(resp: httpx.Response) -> str:
    try:
        j = resp.json()
        return str(j.get("message") or j)[:300]
    except Exception:
        return resp.text[:300]


def setter_directory(client: HubSpotClient) -> dict[str, dict]:
    """value -> {name, email}. Labels come from the Setter property; emails from Owners (id or userId)."""
    labels = client.setter_labels()
    directory: dict[str, dict] = {v: {"name": lbl, "email": None} for v, lbl in labels.items()}
    try:
        owners = client.owners()
    except HubSpotError:
        owners = []
    for o in owners:
        full = " ".join(x for x in (o.get("firstName"), o.get("lastName")) if x).strip()
        for key in {str(o.get("id")), str(o.get("userId"))}:
            if key in ("None", ""):
                continue
            entry = directory.setdefault(key, {"name": full, "email": None})
            entry["email"] = (o.get("email") or "").lower() or entry["email"]
            if not entry["name"]:
                entry["name"] = full
    return directory


def fetch_booking_metrics(client: HubSpotClient, start: datetime, end: datetime) -> BookingResult:
    res = BookingResult()
    prop = settings.setter_property

    # Bookings: contacts on deals that entered Counselling Scheduled in the window.
    booked_deals, booked_prop = client.search_deals_entered_stage(settings.booking_stage_id, start, end)
    deal_setter = {str(d["id"]): (d.get("properties") or {}).get(prop) or UNASSIGNED for d in booked_deals}
    contacts_by_deal = client.deal_contacts(list(deal_setter)) if deal_setter else {}

    all_contacts = sorted({c for cs in contacts_by_deal.values() for c in cs})
    excluded: dict[str, list[str]] = {}
    exclude_ids = {str(x) for x in settings.booking_exclude_list_ids}
    if exclude_ids:
        for cid in all_contacts:
            hit = client.contact_list_ids(cid) & exclude_ids
            if hit:
                excluded[cid] = sorted(hit)

    booking_contacts: dict[str, set[str]] = defaultdict(set)
    booking_deals: dict[str, set[str]] = defaultdict(set)
    deals_without_contacts = []
    for deal_id, setter in deal_setter.items():
        contacts = [c for c in contacts_by_deal.get(deal_id, []) if c not in excluded]
        if not contacts_by_deal.get(deal_id):
            deals_without_contacts.append(deal_id)
        for c in contacts:
            booking_contacts[setter].add(c)
            booking_deals[setter].add(deal_id)

    # CC: deals that entered Counselling Completed in the window.
    cc_deals, cc_prop = client.search_deals_entered_stage(settings.cc_stage_id, start, end)
    cc_by_setter: dict[str, set[str]] = defaultdict(set)
    for d in cc_deals:
        cc_by_setter[(d.get("properties") or {}).get(prop) or UNASSIGNED].add(str(d["id"]))

    directory = setter_directory(client)
    for value in sorted(set(booking_contacts) | set(cc_by_setter)):
        info = directory.get(value, {}) if value != UNASSIGNED else {}
        name = "Unassigned" if value == UNASSIGNED else (info.get("name") or "")
        if value != UNASSIGNED and not name:
            res.warnings.append(f"HubSpot setter value {value} has no name in HubSpot; shown by ID.")
        res.rows.append({
            "hubspot_value": None if value == UNASSIGNED else value,
            "name": name or f"HubSpot user {value}",
            "name_found": bool(name),
            "email": info.get("email"),
            "bookings": len(booking_contacts.get(value, set())),
            "cc": len(cc_by_setter.get(value, set())),
            "booking_contact_ids": sorted(booking_contacts.get(value, set())),
            "booking_deal_ids": sorted(booking_deals.get(value, set())),
            "cc_deal_ids": sorted(cc_by_setter.get(value, set())),
        })

    res.total_bookings = len({c for s in booking_contacts.values() for c in s})
    res.total_cc = len({d for s in cc_by_setter.values() for d in s})
    if deals_without_contacts:
        res.warnings.append(f"{len(deals_without_contacts)} booked deal(s) have no associated contact and "
                            "are not counted (same as the HubSpot report).")
    res.meta = {
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "booking_stage_id": settings.booking_stage_id, "booking_date_property": booked_prop,
        "cc_stage_id": settings.cc_stage_id, "cc_date_property": cc_prop,
        "setter_property": prop, "excluded_list_ids": sorted(exclude_ids),
        "booked_deals": len(deal_setter), "excluded_contacts": excluded,
        "cc_deals": len(cc_deals), "api_requests": client.request_count,
        "source_reports": {"bookings": "Meeting Booking - Daily (155571610)", "cc": "CCs - today (163016740)"},
    }
    return res
