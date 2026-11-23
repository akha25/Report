from __future__ import annotations

import hmac
import html
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .config import settings
from .db import get_session, init_db, session_scope
from .delivery import deliver
from .exports import day_xlsx, snapshot_csv, snapshot_xlsx
from .hubspot import HubSpotClient, HubSpotError
from .models import CsvUpload, Delivery, EventLog, RosterMember, Snapshot, log_event
from .reporting import (compare_snapshots, current_snapshot, day_timeline, generate_snapshot, latest_snapshot,
                        snapshot_dict, store_csv_upload)
from .roster import ROLES, seed_roster
from .timeutil import cycle_cutoff, cycle_label, cycles, is_working_day, now_ist, to_ist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).with_name("static")
MAX_CSV_BYTES = 5 * 1024 * 1024


_ready_lock = threading.Lock()
_state = {"ready": False, "error": ""}


def ensure_ready() -> None:
    """Create tables and seed the roster once per process. Never raises: a failure is shown on the
    setup page instead of crashing the app (important on serverless hosts like Vercel)."""
    if _state["ready"] or settings.setup_problems():
        return
    with _ready_lock:
        if _state["ready"]:
            return
        try:
            init_db()
            with session_scope() as s:
                seed_roster(s)
                if not settings.secret_key:
                    log_event(s, "warning", "system", "SECRET_KEY is not set; sign-ins reset when the app restarts.")
            _state.update(ready=True, error="")
        except Exception as exc:  # database unreachable, wrong URL, etc.
            log.exception("Startup failed")
            _state["error"] = f"Couldn't open the database: {exc.__class__.__name__}: {str(exc)[:300]}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_ready()
    if _state["ready"]:
        scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def setup_guard(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    ensure_ready()
    problems = settings.setup_problems() + ([_state["error"]] if _state["error"] else [])
    if problems:
        items = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
        page = SETUP_PAGE.replace("{{items}}", items)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Setup needed: " + " ".join(problems)}, status_code=503)
        return HTMLResponse(page, status_code=503)
    return await call_next(request)


SETUP_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setup needed</title><style>body{font-family:system-ui,sans-serif;background:#f5f6f8;color:#18212f;display:grid;
place-items:center;min-height:100vh;margin:0}.c{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:28px;
max-width:640px}h1{font-size:20px;margin:0 0 8px}li{margin:10px 0;line-height:1.45}p{color:#667085}</style></head>
<body><div class="c"><h1>Daily Setter &amp; Closer Report: setup needed</h1>
<p>The app is running, but it can't be used until these are fixed:</p><ol>{{items}}</ol>
<p>After changing environment variables or connecting storage, redeploy the project.</p></div></body></html>"""

app.add_middleware(SessionMiddleware, secret_key=settings.secret_key or secrets.token_urlsafe(32),
                   same_site="lax", https_only=settings.secure_cookies, max_age=60 * 60 * 12)


# ------------------------------------------------------------------ auth
def current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "This needs the admin password.")
    return user


class LoginBody(BaseModel):
    name: str = ""
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    role = None
    if settings.admin_password and hmac.compare_digest(body.password, settings.admin_password):
        role = "admin"
    elif settings.viewer_password and hmac.compare_digest(body.password, settings.viewer_password):
        role = "viewer"
    if role is None:
        raise HTTPException(401, "Wrong password." if settings.admin_password else
                            "No password is configured yet. Set ADMIN_PASSWORD and restart.")
    request.session["user"] = {"name": (body.name or role).strip()[:60], "role": role}
    return request.session["user"]


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return user


# ------------------------------------------------------------------ status
@app.get("/healthz")
def healthz():
    return {"ok": True, "time": now_ist().isoformat()}


@app.get("/api/status")
def status(user=Depends(current_user), s: Session = Depends(get_session)):
    today = now_ist().date()
    cyc = []
    for h in cycles():
        snap = current_snapshot(s, today, h)
        sent = s.scalar(select(Delivery).where(Delivery.report_date == today, Delivery.cycle_hour == h,
                                               Delivery.status == "sent").order_by(Delivery.id.desc()).limit(1))
        cyc.append({"hour": h, "label": cycle_label(h), "due": cycle_cutoff(today, h) <= now_ist(),
                    "snapshot_id": snap.id if snap else None, "status": snap.status if snap else None,
                    "calling_status": snap.calling_status if snap else None,
                    "booking_status": snap.booking_status if snap else None,
                    "posted": bool(sent)})
    last_upload = s.scalar(select(CsvUpload).order_by(CsvUpload.uploaded_at.desc()).limit(1))
    unclassified = s.scalars(select(RosterMember).where(RosterMember.role == "unclassified",
                                                        RosterMember.active.is_(True))).all()
    errors = s.scalars(select(EventLog).where(EventLog.level == "error").order_by(EventLog.at.desc()).limit(5)).all()
    return {
        "now": now_ist().isoformat(), "today": today.isoformat(), "working_day": is_working_day(today),
        "working_days": settings.working_days, "timezone": settings.timezone, "cycles": cyc,
        "hubspot_configured": settings.hubspot_configured, "slack_configured": settings.slack_configured,
        "scheduler_enabled": settings.scheduler_enabled, "next_runs": scheduler.next_runs(),
        "auto_deliver": settings.auto_deliver, "tt_decimals": settings.tt_decimals, "csv_wait_minutes": settings.csv_wait_minutes,
        "last_upload": _upload_summary(last_upload) if last_upload else None,
        "unclassified": [m.name for m in unclassified],
        "recent_errors": [_event(e) for e in errors],
        "user": user,
    }


@app.post("/api/hubspot/test")
def hubspot_test(user=Depends(admin_user), s: Session = Depends(get_session)):
    try:
        client = HubSpotClient(max_attempts=2)
        try:
            info = client.test_connection()
        finally:
            client.close()
    except HubSpotError as exc:
        log_event(s, "error", "booking", f"HubSpot connection test failed: {exc}")
        s.commit()
        return {"ok": False, "message": str(exc)}
    log_event(s, "info", "booking", "HubSpot connection test passed.", **info)
    s.commit()
    return {"ok": True, "message": f"Connected. The Setter field has {info['setter_options']} people.", **info}


# ------------------------------------------------------------------ reports
@app.get("/api/reports/latest")
def report_latest(user=Depends(current_user), s: Session = Depends(get_session)):
    snap = latest_snapshot(s)
    return snapshot_dict(snap) if snap else None


@app.get("/api/reports")
def reports_for_date(day: date, user=Depends(current_user), s: Session = Depends(get_session)):
    snaps = s.scalars(select(Snapshot).where(Snapshot.report_date == day)
                      .order_by(Snapshot.cycle_hour, Snapshot.version.desc())).all()
    by_cycle = []
    for h in cycles():
        versions = [x for x in snaps if x.cycle_hour == h]
        by_cycle.append({"hour": h, "label": cycle_label(h), "versions": [
            {"id": v.id, "version": v.version, "status": v.status, "trigger": v.trigger,
             "created_at": to_ist(v.created_at).isoformat(),
             "posted": any(d.status == "sent" for d in v.deliveries)} for v in versions]})
    return {"date": day.isoformat(), "working_day": is_working_day(day), "cycles": by_cycle}


@app.get("/api/dates")
def report_dates(user=Depends(current_user), s: Session = Depends(get_session)):
    rows = s.execute(select(Snapshot.report_date).distinct().order_by(Snapshot.report_date.desc()).limit(120)).all()
    return [r[0].isoformat() for r in rows]


@app.get("/api/reports/{snap_id}")
def report_get(snap_id: int, user=Depends(current_user), s: Session = Depends(get_session)):
    snap = s.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404, "Report not found.")
    return snapshot_dict(snap)


class GenerateBody(BaseModel):
    date: date
    hour: int
    post_to_slack: bool = False


@app.post("/api/reports/generate")
def report_generate(body: GenerateBody, user=Depends(admin_user), s: Session = Depends(get_session)):
    if body.hour not in cycles():
        raise HTTPException(400, f"Hour must be one of {cycles()}.")
    if cycle_cutoff(body.date, body.hour).date() > now_ist().date():
        raise HTTPException(400, "Can't generate a report for a future date.")
    snap = generate_snapshot(s, body.date, body.hour, trigger="manual", triggered_by=user["name"])
    if body.post_to_slack:
        deliver(s, snap, kind="correction" if _already_posted(s, snap) else "report", force=True)
    s.commit()
    return snapshot_dict(snap)


def _already_posted(s: Session, snap: Snapshot) -> bool:
    return s.scalar(select(Delivery.id).where(Delivery.report_date == snap.report_date,
                                              Delivery.cycle_hour == snap.cycle_hour,
                                              Delivery.status == "sent").limit(1)) is not None


@app.post("/api/reports/{snap_id}/deliver")
def report_deliver(snap_id: int, user=Depends(admin_user), s: Session = Depends(get_session)):
    snap = s.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404, "Report not found.")
    if not settings.slack_configured:
        raise HTTPException(400, "Slack isn't set up (SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN + SLACK_CHANNEL_ID).")
    d = deliver(s, snap, kind="correction" if _already_posted(s, snap) else "report", force=True)
    s.commit()
    return {"status": d.status, "detail": d.detail}


@app.get("/api/reports/{snap_id}/download")
def report_download(snap_id: int, format: str = "xlsx", user=Depends(current_user), s: Session = Depends(get_session)):
    snap = s.get(Snapshot, snap_id)
    if not snap:
        raise HTTPException(404, "Report not found.")
    stem = f"setter-closer-report_{snap.report_date}_{cycle_label(snap.cycle_hour).replace(' ', '')}_v{snap.version}"
    if format == "csv":
        return Response(snapshot_csv(snap), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})
    return Response(snapshot_xlsx(snap), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'})


@app.get("/api/days/{day}/download")
def day_download(day: date, user=Depends(current_user), s: Session = Depends(get_session)):
    snaps = [x for h in cycles() if (x := current_snapshot(s, day, h))]
    return Response(day_xlsx(snaps), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="setter-closer-reports_{day}.xlsx"'})


@app.get("/api/compare")
def compare(a: int, b: int, user=Depends(current_user), s: Session = Depends(get_session)):
    sa, sb = s.get(Snapshot, a), s.get(Snapshot, b)
    if not sa or not sb:
        raise HTTPException(404, "Report not found.")
    return compare_snapshots(sa, sb)


@app.get("/api/timeline")
def timeline(day: date, user=Depends(current_user), s: Session = Depends(get_session)):
    return day_timeline(s, day)


# ------------------------------------------------------------------ uploads
def _upload_summary(u: CsvUpload) -> dict:
    return {"id": u.id, "filename": u.filename, "uploaded_at": to_ist(u.uploaded_at).isoformat(),
            "uploaded_by": u.uploaded_by, "source": u.source, "report_date": u.report_date.isoformat(),
            "exported_at": to_ist(u.exported_at).isoformat(), "exported_at_source": u.exported_at_source,
            "valid": u.valid, "people": len(u.rows or []), "column_map": u.column_map, "tt_format": u.tt_format,
            "issues": u.issues}


def _handle_upload(s: Session, raw: bytes, filename: str, by: str, source: str, exported_at: str | None):
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "File is larger than 5 MB.")
    if not filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(400, "Please upload the .csv file exported from FreJun.")
    override = None
    if exported_at:
        try:
            override = datetime.fromisoformat(exported_at)
        except ValueError:
            raise HTTPException(400, "exported_at must look like 2026-11-23T18:57")
    up, dup = store_csv_upload(s, raw, filename, uploaded_by=by, source=source, exported_at_override=override)
    s.commit()
    if up.valid:
        scheduler.notify_csv_uploaded()
        if not scheduler.is_running():
            scheduler.catch_up(trigger="upload")
    return {**_upload_summary(up), "duplicate_file": dup}


@app.post("/api/uploads")
async def upload_csv(file: UploadFile = File(...), exported_at: str | None = Form(None),
                     user=Depends(admin_user), s: Session = Depends(get_session)):
    return _handle_upload(s, await file.read(), file.filename or "upload.csv", user["name"], "dashboard", exported_at)


@app.post("/api/ingest/csv")
async def ingest_csv(file: UploadFile = File(...), exported_at: str | None = Form(None),
                     x_ingest_token: str = Header(""), s: Session = Depends(get_session)):
    if not settings.ingest_token or not hmac.compare_digest(x_ingest_token, settings.ingest_token):
        raise HTTPException(401, "Invalid ingest token.")
    return _handle_upload(s, await file.read(), file.filename or "upload.csv", "automation", "api", exported_at)


@app.get("/api/uploads")
def list_uploads(limit: int = 30, user=Depends(current_user), s: Session = Depends(get_session)):
    ups = s.scalars(select(CsvUpload).order_by(CsvUpload.uploaded_at.desc()).limit(min(limit, 200))).all()
    return [_upload_summary(u) for u in ups]


@app.get("/api/uploads/{upload_id}/raw")
def upload_raw(upload_id: int, user=Depends(current_user), s: Session = Depends(get_session)):
    u = s.get(CsvUpload, upload_id)
    if not u:
        raise HTTPException(404, "Upload not found.")
    return Response(u.raw, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{u.filename}"'})


# ------------------------------------------------------------------ roster
def _member(m: RosterMember) -> dict:
    return {"id": m.id, "name": m.name, "email": m.email, "role": m.role, "team": m.team,
            "hubspot_owner_id": m.hubspot_owner_id, "active": m.active, "aliases": m.aliases, "notes": m.notes}


@app.get("/api/roster")
def roster_list(user=Depends(current_user), s: Session = Depends(get_session)):
    ms = s.scalars(select(RosterMember)).all()
    order = {r: i for i, r in enumerate(("unclassified", "setter", "closer"))}
    return [_member(m) for m in sorted(ms, key=lambda m: (order.get(m.role, 9), not m.active, m.name.lower()))]


class MemberBody(BaseModel):
    name: str | None = None
    email: str | None = None
    role: str | None = None
    team: str | None = None
    active: bool | None = None
    aliases: list[str] | None = None
    notes: str | None = None
    hubspot_owner_id: str | None = None


@app.post("/api/roster")
def roster_add(body: MemberBody, user=Depends(admin_user), s: Session = Depends(get_session)):
    if not body.name:
        raise HTTPException(400, "Name is required.")
    m = RosterMember(name=body.name.strip(), email=(body.email or "").strip().lower() or None,
                     role=body.role or "unclassified", team=body.team or "", aliases=body.aliases or [],
                     notes=body.notes or "", hubspot_owner_id=body.hubspot_owner_id or "")
    if m.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}.")
    s.add(m)
    log_event(s, "info", "roster", f"{user['name']} added {m.name} as {m.role}.")
    s.commit()
    return _member(m)


@app.patch("/api/roster/{member_id}")
def roster_update(member_id: int, body: MemberBody, user=Depends(admin_user), s: Session = Depends(get_session)):
    m = s.get(RosterMember, member_id)
    if not m:
        raise HTTPException(404, "Person not found.")
    changes = body.model_dump(exclude_unset=True)
    if "role" in changes and changes["role"] not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}.")
    if "email" in changes:
        changes["email"] = (changes["email"] or "").strip().lower() or None
    for k, v in changes.items():
        setattr(m, k, v)
    log_event(s, "info", "roster", f"{user['name']} updated {m.name}: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    s.commit()
    return _member(m)


# ------------------------------------------------------------------ logs + cron
def _event(e: EventLog) -> dict:
    return {"id": e.id, "at": to_ist(e.at).isoformat(), "level": e.level, "module": e.module,
            "message": e.message, "detail": e.detail}


@app.get("/api/logs")
def logs(level: str | None = None, module: str | None = None, limit: int = 200,
         user=Depends(current_user), s: Session = Depends(get_session)):
    q = select(EventLog).order_by(EventLog.at.desc(), EventLog.id.desc()).limit(min(limit, 1000))
    if level:
        q = q.where(EventLog.level == level)
    if module:
        q = q.where(EventLog.module == module)
    return [_event(e) for e in s.scalars(q).all()]


@app.api_route("/api/cron/run", methods=["GET", "POST"])
def cron_run(wait: bool = False, x_cron_secret: str = Header(""), authorization: str = Header("")):
    """Called by Vercel Cron (GET, 'Authorization: Bearer <CRON_SECRET>') or any external cron
    (POST, 'X-Cron-Secret'). Creates any of today's due reports that don't exist yet; safe to repeat.
    With ?wait=true a cycle without a fresh FreJun CSV is held back until CSV_WAIT_MINUTES have
    passed (use this when the cron calls every few minutes)."""
    supplied = x_cron_secret or (authorization[7:] if authorization.lower().startswith("bearer ") else "")
    if not settings.cron_secret or not hmac.compare_digest(supplied, settings.cron_secret):
        raise HTTPException(401, "Invalid cron secret.")
    made = scheduler.catch_up(trigger="cron", respect_wait=wait)
    return {"generated": made}


# ------------------------------------------------------------------ frontend
@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
