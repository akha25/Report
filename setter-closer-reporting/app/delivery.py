"""Report delivery to Slack. Each cycle is posted once; a later version is only posted automatically
when it fixes a module that was missing/failed in the earlier post, or when someone sends a correction."""
from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Delivery, Snapshot, log_event
from .timeutil import cycle_label, to_ist


def _fmt_num(v, decimals: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:,.{decimals}f}" if decimals else f"{int(v):,}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    def line(cells):
        return "  ".join(c.ljust(widths[0]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cells))
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), sep, *(line(r) for r in rows)])


def calling_text(snap: Snapshot) -> str:
    if snap.calling_status not in ("ok", "stale"):
        return f"Calling Inputs unavailable: {snap.calling_message}"
    d = settings.tt_decimals
    blocks = []
    secs = snap.calling["sections"]
    for key in ("setter", "closer", "unclassified"):
        sec = secs[key]
        if not sec["rows"]:
            continue
        rows = [[r["name"], _fmt_num(r["calls"]), _fmt_num(r["tt_minutes"], d), _fmt_num(r["connected"]),
                 _fmt_num(r["unique"])] for r in sec["rows"]]
        t = sec["totals"]
        rows.append([f"Total {sec['title']}", _fmt_num(t["calls"]), _fmt_num(t["tt_minutes"], d),
                     _fmt_num(t["connected"]), _fmt_num(t["unique"])])
        blocks.append(f"{sec['title']}\n" + _table(["Name", "Calls", "TT (min)", "Connected", "Unique"], rows))
    t = snap.calling["totals"]["setters_and_closers"]
    blocks.append(f"Setters + Closers: {_fmt_num(t['calls'])} calls · {_fmt_num(t['tt_minutes'], d)} min TT · "
                  f"{_fmt_num(t['connected'])} connected · {_fmt_num(t['unique'])} unique")
    return "\n\n".join(blocks)


def booking_text(snap: Snapshot) -> str:
    if snap.booking_status != "ok":
        return f"Booking Inputs unavailable: {snap.booking_message}"
    rows = [[r["name"], _fmt_num(r["bookings"]), _fmt_num(r["cc"])] for r in snap.booking["rows"]]
    t = snap.booking["totals"]
    rows.append(["Total", _fmt_num(t["bookings"]), _fmt_num(t["cc"])])
    return _table(["Setter Name", "Bookings", "CC"], rows)


def slack_payload(snap: Snapshot, kind: str = "report") -> dict:
    label = cycle_label(snap.cycle_hour)
    prefix = {"report": "", "update": "UPDATE · ", "correction": "CORRECTION · "}[kind]
    title = f"{prefix}Setter & Closer Report · till {label} · {snap.report_date:%a %d %b %Y}"
    notes = []
    if snap.calling_status == "stale":
        notes.append(f":warning: {snap.calling_message}")
    if snap.calling_status in ("missing", "invalid"):
        notes.append(f":x: Calling: {snap.calling_message}")
    if snap.booking_status != "ok":
        notes.append(f":x: Booking: {snap.booking_message}")
    src = (snap.calling or {}).get("source", {})
    context = [f"Generated {to_ist(snap.created_at):%I:%M %p} IST · version {snap.version}"]
    if src:
        context.append(f"FreJun export {to_ist_iso(src.get('exported_at'))}")
    if snap.booking_status == "ok":
        context.append(f"HubSpot up to {to_ist_iso(snap.booking['meta']['window_end'])}")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(context)}]},
    ]
    if notes:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(notes)[:2900]}})
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Calling Inputs* (sorted by TT)"}},
        *[{"type": "section", "text": {"type": "mrkdwn", "text": f"```{part[:2900]}```"}}
          for part in calling_text(snap).split("\n\n")],
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Booking Inputs* (sorted by Bookings)"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{booking_text(snap)[:2900]}```"}},
    ]
    if settings.public_base_url:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f"<{settings.public_base_url}/#/report/{snap.id}|Open in dashboard>"}]})
    return {"text": title, "blocks": blocks}


def to_ist_iso(value: str | None) -> str:
    if not value:
        return "—"
    from datetime import datetime
    return f"{to_ist(datetime.fromisoformat(value)):%I:%M %p}"


def _post(payload: dict, transport: httpx.BaseTransport | None = None) -> str:
    with httpx.Client(timeout=20.0, transport=transport) as client:
        if settings.slack_webhook_url:
            r = client.post(settings.slack_webhook_url, json=payload)
            if r.status_code >= 300:
                raise RuntimeError(f"Slack webhook returned {r.status_code}: {r.text[:200]}")
            return "webhook ok"
        r = client.post("https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                        json={"channel": settings.slack_channel_id, **payload})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack error: {data.get('error')}")
        return f"ts={data.get('ts')}"


def decide_kind(session: Session, snap: Snapshot) -> str | None:
    """Return 'report' / 'update' to send automatically, or None if this cycle was already posted."""
    previous = session.scalars(select(Delivery).where(
        Delivery.report_date == snap.report_date, Delivery.cycle_hour == snap.cycle_hour,
        Delivery.status == "sent").order_by(Delivery.id.desc())).all()
    if not previous:
        return "report"
    last = previous[0].snapshot
    fixed = (last.status != "complete" and snap.status == "complete") or \
            (last.booking_status != "ok" and snap.booking_status == "ok") or \
            (last.calling_status not in ("ok",) and snap.calling_status == "ok")
    return "update" if fixed else None


def deliver(session: Session, snap: Snapshot, kind: str | None = None, force: bool = False,
            transport: httpx.BaseTransport | None = None) -> Delivery | None:
    if not settings.slack_configured:
        return None
    if snap.status == "failed" and not force:
        log_event(session, "warning", "delivery", f"{cycle_label(snap.cycle_hour)} report not posted: both sources failed.",
                  snapshot_id=snap.id)
        return None
    if not force:
        kind = decide_kind(session, snap)
        if kind is None:
            log_event(session, "info", "delivery", f"{cycle_label(snap.cycle_hour)} v{snap.version} not posted: "
                      "this cycle was already posted to Slack.", snapshot_id=snap.id)
            return None
    kind = kind or "correction"
    try:
        detail = _post(slack_payload(snap, kind), transport)
        status = "sent"
        log_event(session, "info", "delivery", f"Posted {cycle_label(snap.cycle_hour)} report v{snap.version} to Slack ({kind}).",
                  snapshot_id=snap.id)
    except Exception as exc:
        detail, status = str(exc)[:500], "failed"
        log_event(session, "error", "delivery", f"Slack post failed: {detail}", snapshot_id=snap.id)
    d = Delivery(snapshot_id=snap.id, report_date=snap.report_date, cycle_hour=snap.cycle_hour,
                 kind=kind, status=status, detail=detail)
    session.add(d)
    session.flush()
    return d
