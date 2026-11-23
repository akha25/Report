"""Builds report snapshots. Calling and Booking are built independently: if one source fails, the
other is still saved and the failed module is shown as failed (never filled with guesses)."""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .csv_parser import parse_frejun_csv, seconds_to_minutes
from .hubspot import UNASSIGNED, HubSpotClient, HubSpotError, fetch_booking_metrics
from .models import CsvUpload, RosterMember, Snapshot, log_event
from .roster import RosterIndex, register_new_people
from .timeutil import (cycle_cutoff, cycle_label, cycles, day_start, now_ist, previous_cycle_cutoff,
                       to_ist)

SECTION_ORDER = ("setter", "closer", "unclassified")
SECTION_TITLES = {"setter": "Setters", "closer": "Closers", "unclassified": "Role not set"}
_generate_lock = threading.Lock()


# ---------------------------------------------------------------- CSV uploads
def store_csv_upload(session: Session, raw: bytes, filename: str, uploaded_by: str = "",
                     source: str = "dashboard", exported_at_override: datetime | None = None) -> tuple[CsvUpload, bool]:
    """Returns (upload, is_duplicate_file)."""
    uploaded_at = now_ist()
    parsed = parse_frejun_csv(raw, filename, uploaded_at, exported_at_override)
    existing = session.scalar(select(CsvUpload).where(CsvUpload.sha256 == parsed.sha256))
    if existing and not exported_at_override:
        log_event(session, "info", "calling", f"'{filename}' was already uploaded (upload #{existing.id}); not stored again.")
        return existing, True
    up = CsvUpload(
        uploaded_at=uploaded_at, uploaded_by=uploaded_by, source=source, filename=filename,
        sha256=parsed.sha256, raw=raw, report_date=parsed.report_date, exported_at=parsed.exported_at,
        exported_at_source=parsed.exported_at_source, valid=parsed.valid, column_map=parsed.column_map,
        tt_format=parsed.tt_format, rows=parsed.rows, issues=parsed.issues,
    )
    session.add(up)
    session.flush()
    if parsed.valid:
        register_new_people(session, parsed.rows)
        log_event(session, "info", "calling",
                  f"CSV uploaded: {filename} ({len(parsed.rows)} people, exported "
                  f"{parsed.exported_at:%d %b %I:%M %p}).", upload_id=up.id)
    else:
        errors = [i["message"] for i in parsed.issues if i["level"] == "error"]
        log_event(session, "error", "calling", f"CSV rejected: {filename}. " + " ".join(errors), upload_id=up.id)
    return up, False


def select_csv_for_cycle(session: Session, report_date: date, cutoff: datetime) -> CsvUpload | None:
    limit = cutoff + timedelta(minutes=settings.csv_grace_minutes)
    candidates = session.scalars(
        select(CsvUpload).where(CsvUpload.report_date == report_date, CsvUpload.valid.is_(True))
        .order_by(CsvUpload.exported_at.desc(), CsvUpload.uploaded_at.desc())
    ).all()
    for c in candidates:
        if to_ist(c.exported_at) <= limit:
            return c
    return None


def csv_is_fresh(upload: CsvUpload | None, report_date: date, hour: int) -> bool:
    return bool(upload) and to_ist(upload.exported_at) > previous_cycle_cutoff(report_date, hour)


# ---------------------------------------------------------------- Calling
def _calling_sort_key(r: dict):
    tt = r.get("tt_seconds")
    return (tt is None, -(tt or 0), -(r.get("calls") or 0), r["name"].lower())


def _totals(rows: list[dict]) -> dict:
    def s(k):
        vals = [r[k] for r in rows if r.get(k) is not None]
        return sum(vals) if vals else 0
    tt = s("tt_seconds")
    return {"people": len(rows), "calls": s("calls"), "tt_seconds": tt,
            "tt_minutes": seconds_to_minutes(tt), "connected": s("connected"), "unique": s("unique"),
            "rows_missing_tt": sum(1 for r in rows if r.get("tt_seconds") is None)}


def build_calling(session: Session, upload: CsvUpload | None, report_date: date, hour: int) -> tuple[str, str, dict]:
    if upload is None:
        return "missing", "No FreJun CSV for this date has been uploaded yet.", {}
    roster = RosterIndex.load(session)
    sections: dict[str, list[dict]] = {k: [] for k in SECTION_ORDER}
    hidden_inactive, hidden_idle = [], []
    for src in upload.rows:
        member, method = roster.match(src["name"], src.get("email"))
        role = member.role if member else "unclassified"
        active_flag = any((src.get(k) or 0) > 0 for k in ("calls", "tt_seconds", "connected", "unique"))
        if member and not member.active:
            hidden_inactive.append(src["name"])
            continue
        if role == "unclassified" and not active_flag:
            hidden_idle.append(src["name"])
            continue
        sections[role].append({
            "name": src["name"], "email": src.get("email"), "role": role,
            "team": member.team if member else "", "roster_id": member.id if member else None,
            "match": method, "calls": src.get("calls"), "tt_seconds": src.get("tt_seconds"),
            "tt_minutes": src.get("tt_minutes"), "connected": src.get("connected"),
            "unique": src.get("unique"), "flags": src.get("flags", []), "line": src.get("line"),
        })
    out_sections = {}
    for k in SECTION_ORDER:
        rows = sorted(sections[k], key=_calling_sort_key)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        out_sections[k] = {"title": SECTION_TITLES[k], "rows": rows, "totals": _totals(rows)}
    all_rows = [r for k in SECTION_ORDER for r in sections[k]]
    sc = [r for k in ("setter", "closer") for r in sections[k]]
    calling = {
        "sections": out_sections,
        "totals": {"setters_and_closers": _totals(sc), "all_shown": _totals(all_rows)},
        "hidden": {"no_activity_role_not_set": hidden_idle, "inactive": hidden_inactive},
        "source": {"upload_id": upload.id, "filename": upload.filename, "sha256": upload.sha256,
                   "exported_at": to_ist(upload.exported_at).isoformat(),
                   "exported_at_source": upload.exported_at_source, "column_map": upload.column_map,
                   "tt_format": upload.tt_format, "tt_rule": "TT = 'Total duration' converted to minutes "
                   f"(seconds / 60, shown to {settings.tt_decimals} decimal place(s)); sorted on exact seconds."},
        "upload_issues": upload.issues,
    }
    if not csv_is_fresh(upload, report_date, hour):
        prev = previous_cycle_cutoff(report_date, hour)
        return ("stale", f"No new FreJun CSV since {prev:%I:%M %p}. Using the export from "
                f"{to_ist(upload.exported_at):%I:%M %p}.", calling)
    return "ok", "", calling


# ---------------------------------------------------------------- Booking
def _booking_sort_key(r: dict):
    return (r.get("hubspot_value") is None and r["name"] == "Unassigned", -r["bookings"], -r["cc"], r["name"].lower())


def build_booking(session: Session, report_date: date, end: datetime,
                  client_factory: Callable[[], HubSpotClient] | None = None) -> tuple[str, str, dict]:
    if not settings.hubspot_configured and client_factory is None:
        return "not_configured", "HubSpot isn't connected yet (HUBSPOT_TOKEN missing).", {}
    try:
        client = (client_factory or HubSpotClient)()
    except HubSpotError as exc:
        return "failed", str(exc), {}
    try:
        res = fetch_booking_metrics(client, day_start(report_date), end)
    except HubSpotError as exc:
        return "failed", str(exc), {}
    except Exception as exc:  # unexpected payloads etc.
        return "failed", f"Unexpected error reading HubSpot: {exc.__class__.__name__}: {exc}", {}
    finally:
        client.close()

    roster = RosterIndex.load(session)
    warnings = list(res.warnings)
    rows = []
    seen_members = set()
    for r in res.rows:
        member, method = roster.match(r["name"] if r["name_found"] else None, r.get("email"), r.get("hubspot_value"))
        if member is not None:
            seen_members.add(member.id)
            if r.get("hubspot_value") and member.hubspot_owner_id != r["hubspot_value"]:
                member.hubspot_owner_id = r["hubspot_value"]  # learn the link for next time
        elif r.get("hubspot_value"):
            warnings.append(f"{r['name']} (HubSpot) isn't matched to anyone on the roster.")
        rows.append({**r, "role": member.role if member else "unclassified", "roster_id": member.id if member else None,
                     "match": method, "zero_filled": False})

    if settings.include_zero_setters:
        for m in session.scalars(select(RosterMember).where(RosterMember.role == "setter",
                                                            RosterMember.active.is_(True))).all():
            if m.id not in seen_members:
                rows.append({"hubspot_value": m.hubspot_owner_id or None, "name": m.name, "name_found": True,
                             "email": m.email, "bookings": 0, "cc": 0, "booking_contact_ids": [],
                             "booking_deal_ids": [], "cc_deal_ids": [], "role": "setter", "roster_id": m.id,
                             "match": "roster", "zero_filled": True})

    rows.sort(key=_booking_sort_key)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    booking = {
        "rows": rows,
        "totals": {"bookings": res.total_bookings, "cc": res.total_cc,
                   "bookings_row_sum": sum(r["bookings"] for r in rows), "cc_row_sum": sum(r["cc"] for r in rows)},
        "meta": res.meta, "warnings": warnings,
    }
    return "ok", "", booking


# ---------------------------------------------------------------- Snapshots
def generate_snapshot(session: Session, report_date: date, hour: int, trigger: str = "manual",
                      triggered_by: str = "system", now: datetime | None = None,
                      client_factory: Callable[[], HubSpotClient] | None = None) -> Snapshot:
    with _generate_lock:
        now = to_ist(now) if now else now_ist()
        cutoff = cycle_cutoff(report_date, hour)
        hubspot_end = min(cutoff, now)
        issues: list[dict] = []
        if hubspot_end < cutoff:
            issues.append({"level": "info", "module": "booking",
                           "message": f"Run before {cycle_label(hour)}: HubSpot numbers are up to {now:%I:%M %p}."})

        upload = select_csv_for_cycle(session, report_date, min(cutoff, now) if now < cutoff else cutoff)
        c_status, c_msg, calling = build_calling(session, upload, report_date, hour)
        b_status, b_msg, booking = build_booking(session, report_date, hubspot_end, client_factory)

        for w in booking.get("warnings", []):
            issues.append({"level": "warning", "module": "booking", "message": w})
        for i in (calling.get("upload_issues") or []):
            if i["level"] != "info":
                issues.append({"level": i["level"], "module": "calling", "message": i["message"]})

        ok_calling, ok_booking = c_status == "ok", b_status == "ok"
        usable_calling = c_status in ("ok", "stale")
        status = "complete" if ok_calling and ok_booking else ("partial" if usable_calling or ok_booking else "failed")

        for attempt in range(3):
            version = (session.scalar(select(func.max(Snapshot.version)).where(
                Snapshot.report_date == report_date, Snapshot.cycle_hour == hour)) or 0) + 1
            snap = Snapshot(report_date=report_date, cycle_hour=hour, version=version, trigger=trigger,
                            triggered_by=triggered_by, created_at=now, cutoff_at=cutoff, status=status,
                            calling_status=c_status, calling_message=c_msg,
                            csv_upload_id=upload.id if upload else None, calling=calling,
                            booking_status=b_status, booking_message=b_msg, booking=booking, issues=issues)
            try:
                with session.begin_nested():
                    session.add(snap)
                    session.flush()
                break
            except IntegrityError:
                continue
        level = {"complete": "info", "partial": "warning", "failed": "error"}[status]
        parts = [f"Calling: {c_status}" + (f" ({c_msg})" if c_msg else ""),
                 f"Booking: {b_status}" + (f" ({b_msg})" if b_msg else "")]
        log_event(session, level, "system", f"{cycle_label(hour)} report v{snap.version} for {report_date:%d %b %Y} "
                  f"is {status}. " + "; ".join(parts), snapshot_id=snap.id, trigger=trigger)
        if b_status == "failed":
            log_event(session, "error", "booking", b_msg, snapshot_id=snap.id)
        if c_status in ("missing", "stale"):
            log_event(session, "warning", "calling", c_msg, snapshot_id=snap.id)
        return snap


def current_snapshot(session: Session, report_date: date, hour: int) -> Snapshot | None:
    return session.scalar(select(Snapshot).where(Snapshot.report_date == report_date, Snapshot.cycle_hour == hour)
                          .order_by(Snapshot.version.desc()).limit(1))


def latest_snapshot(session: Session) -> Snapshot | None:
    return session.scalar(select(Snapshot).order_by(Snapshot.report_date.desc(), Snapshot.cycle_hour.desc(),
                                                    Snapshot.version.desc()).limit(1))


def snapshot_dict(s: Snapshot, include_trace: bool = True) -> dict:
    booking = s.booking or {}
    if not include_trace and booking.get("rows"):
        booking = {**booking, "rows": [{k: v for k, v in r.items() if not k.endswith("_ids")} for r in booking["rows"]]}
    return {
        "id": s.id, "report_date": s.report_date.isoformat(), "cycle_hour": s.cycle_hour,
        "cycle_label": cycle_label(s.cycle_hour), "version": s.version, "trigger": s.trigger,
        "triggered_by": s.triggered_by, "created_at": to_ist(s.created_at).isoformat(),
        "cutoff_at": to_ist(s.cutoff_at).isoformat(), "status": s.status,
        "calling_status": s.calling_status, "calling_message": s.calling_message, "calling": s.calling,
        "booking_status": s.booking_status, "booking_message": s.booking_message, "booking": booking,
        "issues": s.issues,
        "deliveries": [{"id": d.id, "status": d.status, "kind": d.kind, "detail": d.detail,
                        "sent_at": to_ist(d.sent_at).isoformat()} for d in s.deliveries],
        "hubspot_portal_id": settings.hubspot_portal_id,
        "tt_decimals": settings.tt_decimals,
    }


# ---------------------------------------------------------------- Comparisons
def _person_key(r: dict) -> str:
    return (r.get("email") or "").lower() or r["name"].strip().lower()


def calling_people(s: Snapshot) -> dict[str, dict]:
    out = {}
    for sec in (s.calling or {}).get("sections", {}).values():
        for r in sec["rows"]:
            out[_person_key(r)] = r
    return out


def booking_people(s: Snapshot) -> dict[str, dict]:
    def key(r):
        if r.get("roster_id"):
            return f"roster:{r['roster_id']}"
        return r.get("hubspot_value") or r["name"].lower()
    return {key(r): r for r in (s.booking or {}).get("rows", [])}


def compare_snapshots(a: Snapshot, b: Snapshot) -> dict:
    """b minus a, per person."""
    def delta(x, y):
        return None if x is None or y is None else round(y - x, 4)

    ca, cb = calling_people(a), calling_people(b)
    calling = []
    for key in sorted(set(ca) | set(cb)):
        ra, rb = ca.get(key), cb.get(key)
        ref = rb or ra
        row = {"name": ref["name"], "role": ref["role"]}
        for k in ("calls", "tt_minutes", "connected", "unique"):
            va, vb = (ra or {}).get(k), (rb or {}).get(k)
            row[k] = {"from": va, "to": vb, "delta": delta(va, vb)}
        ta, tb = (ra or {}).get("tt_seconds"), (rb or {}).get("tt_seconds")
        row["tt_minutes"]["delta"] = None if ta is None or tb is None else seconds_to_minutes(tb - ta)
        calling.append(row)
    calling.sort(key=lambda r: -(r["tt_minutes"]["delta"] or 0))

    ba, bb = booking_people(a), booking_people(b)
    booking = []
    for key in sorted(set(ba) | set(bb)):
        ra, rb = ba.get(key), bb.get(key)
        ref = rb or ra
        row = {"name": ref["name"]}
        for k in ("bookings", "cc"):
            va, vb = (ra or {}).get(k), (rb or {}).get(k)
            row[k] = {"from": va, "to": vb, "delta": delta(va, vb)}
        booking.append(row)
    booking.sort(key=lambda r: -(r["bookings"]["delta"] or 0))

    def tot(s):
        c = (s.calling or {}).get("totals", {}).get("setters_and_closers", {})
        bt = (s.booking or {}).get("totals", {})
        return {"calls": c.get("calls"), "tt_minutes": c.get("tt_minutes"), "connected": c.get("connected"),
                "unique": c.get("unique"), "bookings": bt.get("bookings"), "cc": bt.get("cc")}

    ta, tb = tot(a), tot(b)
    return {"from": {"id": a.id, "label": f"{a.report_date:%d %b} {cycle_label(a.cycle_hour)} v{a.version}",
                     "calling_status": a.calling_status, "booking_status": a.booking_status},
            "to": {"id": b.id, "label": f"{b.report_date:%d %b} {cycle_label(b.cycle_hour)} v{b.version}",
                   "calling_status": b.calling_status, "booking_status": b.booking_status},
            "totals": {k: {"from": ta[k], "to": tb[k], "delta": delta(ta[k], tb[k])} for k in ta},
            "calling": calling, "booking": booking}


def day_timeline(session: Session, report_date: date) -> dict:
    """Each person's TT and bookings at every cycle of the day (latest version of each cycle)."""
    snaps = {h: current_snapshot(session, report_date, h) for h in cycles()}
    people: dict[str, dict] = {}
    for h, s in snaps.items():
        if not s:
            continue
        for key, r in calling_people(s).items():
            p = people.setdefault(key, {"name": r["name"], "role": r["role"], "tt": {}, "calls": {}, "bookings": {}, "cc": {}})
            p["tt"][h] = r.get("tt_minutes")
            p["calls"][h] = r.get("calls")
        for r in (s.booking or {}).get("rows", []):
            key = (r.get("email") or "").lower() or r["name"].strip().lower()
            p = people.setdefault(key, {"name": r["name"], "role": r.get("role", "setter"), "tt": {}, "calls": {}, "bookings": {}, "cc": {}})
            p["bookings"][h] = r["bookings"]
            p["cc"][h] = r["cc"]
    return {"date": report_date.isoformat(), "cycles": [{"hour": h, "label": cycle_label(h),
            "snapshot_id": s.id if s else None, "status": s.status if s else None} for h, s in snaps.items()],
            "people": sorted(people.values(), key=lambda p: (p["role"] != "setter", p["name"].lower()))}
