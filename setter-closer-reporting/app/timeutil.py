"""Reporting calendar: IST, Monday–Saturday, cycles at the configured hours (default 2/5/7/9 PM)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.timezone)
UTC = ZoneInfo("UTC")
_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def now_ist() -> datetime:
    return datetime.now(TZ)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(TZ)


def working_weekdays(spec: str | None = None) -> set[int]:
    spec = (spec or settings.working_days).lower().replace(" ", "")
    days: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            for i in range(_DAY_INDEX[a], _DAY_INDEX[b] + 1):
                days.add(i)
        elif part:
            days.add(_DAY_INDEX[part])
    return days


def is_working_day(d: date) -> bool:
    return d.weekday() in working_weekdays()


def cycle_label(hour: int) -> str:
    h12 = hour % 12 or 12
    return f"{h12} {'PM' if hour >= 12 else 'AM'}"


def cycle_key(hour: int) -> str:
    return f"{hour:02d}00"


def cycle_cutoff(d: date, hour: int) -> datetime:
    return datetime.combine(d, time(hour=hour), tzinfo=TZ)


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=TZ)


def cycles() -> list[int]:
    return sorted(settings.cycle_hours)


def previous_cycle_cutoff(d: date, hour: int) -> datetime:
    earlier = [h for h in cycles() if h < hour]
    return cycle_cutoff(d, earlier[-1]) if earlier else day_start(d)


def due_cycles(at: datetime | None = None) -> list[tuple[date, int]]:
    """Cycles for today whose time has passed (used for catch-up after downtime)."""
    at = to_ist(at) if at else now_ist()
    d = at.date()
    if not is_working_day(d):
        return []
    return [(d, h) for h in cycles() if cycle_cutoff(d, h) <= at]


def next_cycle(at: datetime | None = None) -> tuple[date, int]:
    at = to_ist(at) if at else now_ist()
    d = at.date()
    for _ in range(8):
        if is_working_day(d):
            for h in cycles():
                if cycle_cutoff(d, h) > at:
                    return d, h
        d = d + timedelta(days=1)
        at = day_start(d)
    raise RuntimeError("No working day configured")
