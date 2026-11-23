"""Scheduled report generation: Mon–Sat at the configured hours (default 2/5/7/9 PM IST).

At each time:
  1. If no FreJun CSV newer than the previous report has arrived, wait up to CSV_WAIT_MINUTES
     (checking every minute). Uploading a CSV during the wait releases it straight away.
  2. Generate the snapshot (HubSpot counted up to the exact cycle time).
  3. Post it to Slack (once per cycle).
  4. If HubSpot failed, try again after AUTO_RETRY_MINUTES (once) and post an update if it works.

Catch-up: on start-up, and whenever /api/cron/run is called, any of today's past cycles that has no
scheduled report yet is generated (only the most recent one is posted, to avoid flooding Slack).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from .config import settings
from .db import session_scope
from .delivery import deliver
from .models import Snapshot, log_event
from .reporting import csv_is_fresh, generate_snapshot, select_csv_for_cycle
from .timeutil import TZ, cycle_cutoff, cycle_label, cycles, due_cycles, is_working_day, now_ist

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None
_csv_arrived = threading.Event()
SCHEDULED_TRIGGERS = ("scheduled", "catch-up", "cron", "upload")


def notify_csv_uploaded() -> None:
    _csv_arrived.set()


def _has_scheduled_snapshot(session, d: date, hour: int) -> bool:
    return session.scalar(select(Snapshot.id).where(
        Snapshot.report_date == d, Snapshot.cycle_hour == hour,
        Snapshot.trigger.in_(SCHEDULED_TRIGGERS)).limit(1)) is not None


def _wait_for_csv(d: date, hour: int, wait_minutes: int) -> None:
    deadline = _time.monotonic() + wait_minutes * 60
    while _time.monotonic() < deadline:
        with session_scope() as s:
            if csv_is_fresh(select_csv_for_cycle(s, d, cycle_cutoff(d, hour)), d, hour):
                return
        _csv_arrived.clear()
        _csv_arrived.wait(timeout=60)


def run_cycle(hour: int, d: date | None = None, trigger: str = "scheduled", wait: bool = True,
              post: bool = True, client_factory=None) -> int | None:
    d = d or now_ist().date()
    if not is_working_day(d):
        return None
    with session_scope() as s:
        if _has_scheduled_snapshot(s, d, hour):
            return None
        log_event(s, "info", "scheduler", f"{cycle_label(hour)} cycle started ({trigger}).")
    if wait and settings.csv_wait_minutes > 0:
        _wait_for_csv(d, hour, settings.csv_wait_minutes)
    with session_scope() as s:
        if _has_scheduled_snapshot(s, d, hour):  # another worker finished first
            return None
        snap = generate_snapshot(s, d, hour, trigger=trigger, client_factory=client_factory)
        if post and settings.auto_deliver:
            deliver(s, snap)
        snap_id, booking_failed = snap.id, snap.booking_status == "failed"
    if booking_failed and settings.auto_retry_minutes > 0 and _scheduler is not None:
        _scheduler.add_job(retry_cycle, DateTrigger(run_date=now_ist() + timedelta(minutes=settings.auto_retry_minutes)),
                           args=[hour, d.isoformat()], id=f"retry-{d}-{hour}", replace_existing=True)
    return snap_id


def retry_cycle(hour: int, d_iso: str, client_factory=None) -> int | None:
    d = date.fromisoformat(d_iso)
    with session_scope() as s:
        snap = generate_snapshot(s, d, hour, trigger="auto-retry", client_factory=client_factory)
        if settings.auto_deliver:
            deliver(s, snap)
        return snap.id


def catch_up(trigger: str = "catch-up", at: datetime | None = None, client_factory=None,
             respect_wait: bool = False) -> list[int]:
    """Generate any of today's due cycles that have no scheduled report yet.
    respect_wait: hold back a cycle that has no fresh CSV until CSV_WAIT_MINUTES after its time."""
    now = at or now_ist()
    due = due_cycles(now)
    if not due:
        return []
    with session_scope() as s:
        missing = []
        for d, h in due:
            if _has_scheduled_snapshot(s, d, h):
                continue
            if respect_wait and now < cycle_cutoff(d, h) + timedelta(minutes=settings.csv_wait_minutes) \
                    and not csv_is_fresh(select_csv_for_cycle(s, d, cycle_cutoff(d, h)), d, h):
                continue
            missing.append((d, h))
    made = []
    for i, (d, h) in enumerate(missing):
        latest = i == len(missing) - 1 and (d, h) == due[-1]
        sid = run_cycle(h, d, trigger=trigger, wait=False, post=latest, client_factory=client_factory)
        if sid:
            made.append(sid)
    return made


def is_running() -> bool:
    return _scheduler is not None


def start() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled or _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(timezone=TZ, job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 3600})
    for h in cycles():
        sched.add_job(run_cycle, CronTrigger(day_of_week=settings.working_days, hour=h, minute=0, timezone=TZ),
                      args=[h], id=f"cycle-{h}", replace_existing=True)
    sched.start()
    _scheduler = sched
    threading.Thread(target=_safe_catch_up, daemon=True).start()
    log.info("Scheduler started: %s at %s", settings.working_days, [cycle_label(h) for h in cycles()])
    return sched


def _safe_catch_up():
    try:
        catch_up()
    except Exception:  # pragma: no cover
        log.exception("Catch-up failed")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_runs(limit: int = 4) -> list[str]:
    if _scheduler is None:
        return []
    times = sorted(j.next_run_time for j in _scheduler.get_jobs() if j.next_run_time)
    return [t.astimezone(TZ).isoformat() for t in times[:limit]]
