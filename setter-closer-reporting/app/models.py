from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base
from .timeutil import UTC, now_ist


class TZDateTime(TypeDecorator):
    """Stores UTC, always returns timezone-aware datetimes (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now() -> datetime:
    return now_ist()


class CsvUpload(Base):
    """An original FreJun CSV, stored byte-for-byte so every calling number is traceable."""

    __tablename__ = "csv_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uploaded_at: Mapped[datetime] = mapped_column(TZDateTime, default=_now)
    uploaded_by: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(40), default="dashboard")  # dashboard | api
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    raw: Mapped[bytes] = mapped_column(LargeBinary)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    exported_at: Mapped[datetime] = mapped_column(TZDateTime)
    exported_at_source: Mapped[str] = mapped_column(String(40))  # filename | upload_time | manual
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    column_map: Mapped[dict] = mapped_column(JSON, default=dict)
    tt_format: Mapped[str] = mapped_column(String(60), default="")
    rows: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)


class RosterMember(Base):
    """Who is a setter or a closer. Names in reports always come from the source data, not from here."""

    __tablename__ = "roster"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="unclassified")  # setter | closer | unclassified
    team: Mapped[str] = mapped_column(String(80), default="")
    hubspot_owner_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=_now, onupdate=_now)


class Snapshot(Base):
    """One generated report for one date + cycle. Rows are never updated after creation;
    re-running a cycle creates a new version and earlier versions stay in history."""

    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("report_date", "cycle_hour", "version", name="uq_snapshot_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    cycle_hour: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    trigger: Mapped[str] = mapped_column(String(30))  # scheduled | manual | auto-retry | catch-up | cron
    triggered_by: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=_now)
    cutoff_at: Mapped[datetime] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(String(20))  # complete | partial | failed

    calling_status: Mapped[str] = mapped_column(String(20))  # ok | stale | missing | invalid
    calling_message: Mapped[str] = mapped_column(Text, default="")
    csv_upload_id: Mapped[int | None] = mapped_column(ForeignKey("csv_uploads.id"), nullable=True)
    calling: Mapped[dict] = mapped_column(JSON, default=dict)

    booking_status: Mapped[str] = mapped_column(String(20))  # ok | failed | not_configured
    booking_message: Mapped[str] = mapped_column(Text, default="")
    booking: Mapped[dict] = mapped_column(JSON, default=dict)

    issues: Mapped[list] = mapped_column(JSON, default=list)

    csv_upload: Mapped[CsvUpload | None] = relationship()
    deliveries: Mapped[list["Delivery"]] = relationship(back_populates="snapshot", order_by="Delivery.id")


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    cycle_hour: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(40), default="slack")
    kind: Mapped[str] = mapped_column(String(20), default="report")  # report | update | correction
    status: Mapped[str] = mapped_column(String(20))  # sent | failed | skipped
    detail: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(TZDateTime, default=_now)

    snapshot: Mapped[Snapshot] = relationship(back_populates="deliveries")


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZDateTime, default=_now, index=True)
    level: Mapped[str] = mapped_column(String(10))  # info | warning | error
    module: Mapped[str] = mapped_column(String(30))  # calling | booking | scheduler | delivery | system | roster
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class KeyValue(Base):
    """Small persistent state (e.g. cached HubSpot owner names)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=_now, onupdate=_now)


def log_event(session, level: str, module: str, message: str, **detail) -> EventLog:
    ev = EventLog(level=level, module=module, message=message, detail=detail or {})
    session.add(ev)
    return ev
