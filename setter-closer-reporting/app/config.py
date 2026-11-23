"""All settings come from environment variables (or a .env file). Nothing secret is hardcoded."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(os.environ.get("ENV_FILE", ".env"))
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_list(name: str, default: str) -> list[int]:
    raw = os.environ.get(name, default) or default
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass
class Settings:
    # App
    app_name: str = "Daily Setter & Closer Performance Report"
    timezone: str = os.environ.get("REPORT_TIMEZONE", "Asia/Kolkata")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./data/reporting.db")
    secret_key: str = os.environ.get("SECRET_KEY", "")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    viewer_password: str = os.environ.get("VIEWER_PASSWORD", "")
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    secure_cookies: bool = _bool("SECURE_COOKIES", False)

    # Schedule
    cycle_hours: list[int] = field(default_factory=lambda: _int_list("REPORT_HOURS", "14,17,19,21"))
    working_days: str = os.environ.get("WORKING_DAYS", "mon-sat")
    scheduler_enabled: bool = _bool("SCHEDULER_ENABLED", True)
    csv_wait_minutes: int = int(os.environ.get("CSV_WAIT_MINUTES", "20"))
    csv_grace_minutes: int = int(os.environ.get("CSV_GRACE_MINUTES", "15"))
    auto_retry_minutes: int = int(os.environ.get("AUTO_RETRY_MINUTES", "10"))
    cron_secret: str = os.environ.get("CRON_SECRET", "")
    ingest_token: str = os.environ.get("INGEST_TOKEN", "")

    # Calling report
    tt_decimals: int = int(os.environ.get("TT_DECIMALS", "1"))
    plain_number_tt_unit: str = os.environ.get("PLAIN_NUMBER_TT_UNIT", "seconds")  # seconds | minutes

    # HubSpot
    hubspot_token: str = os.environ.get("HUBSPOT_TOKEN", "")
    hubspot_portal_id: str = os.environ.get("HUBSPOT_PORTAL_ID", "48613855")
    hubspot_base_url: str = os.environ.get("HUBSPOT_BASE_URL", "https://api.hubapi.com")
    setter_property: str = os.environ.get("HUBSPOT_SETTER_PROPERTY", "asst_counsellor")
    booking_stage_id: str = os.environ.get("HUBSPOT_BOOKING_STAGE_ID", "1048885393")  # Counselling Scheduled
    cc_stage_id: str = os.environ.get("HUBSPOT_CC_STAGE_ID", "1133451100")  # Counselling Completed
    booking_exclude_list_ids: list[int] = field(
        default_factory=lambda: _int_list("HUBSPOT_BOOKING_EXCLUDE_LIST_IDS", "1724,6614")
    )
    include_zero_setters: bool = _bool("INCLUDE_ZERO_SETTERS", True)

    # Delivery
    slack_webhook_url: str = os.environ.get("SLACK_WEBHOOK_URL", "")
    slack_bot_token: str = os.environ.get("SLACK_BOT_TOKEN", "")
    slack_channel_id: str = os.environ.get("SLACK_CHANNEL_ID", "")
    auto_deliver: bool = _bool("AUTO_DELIVER", True)

    @property
    def hubspot_configured(self) -> bool:
        return bool(self.hubspot_token)

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_webhook_url or (self.slack_bot_token and self.slack_channel_id))


settings = Settings()
