import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from conftest import REAL_CSV_NAME


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(c, pw="admin-pass", name="Ops"):
    return c.post("/api/login", json={"name": name, "password": pw})


def test_requires_login(client):
    assert client.get("/api/status").status_code == 401
    assert login(client, "wrong").status_code == 401
    assert login(client).json() == {"name": "Ops", "role": "admin"}
    assert client.get("/api/status").status_code == 200


def test_full_flow(client, real_csv_bytes, monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler, "catch_up", lambda **kw: [])  # keep this test independent of the clock
    login(client)
    r = client.post("/api/uploads", files={"file": (REAL_CSV_NAME, real_csv_bytes, "text/csv")})
    assert r.status_code == 200 and r.json()["valid"] and r.json()["people"] == 41
    assert client.post("/api/uploads", files={"file": (REAL_CSV_NAME, real_csv_bytes, "text/csv")}).json()["duplicate_file"]

    snap = client.post("/api/reports/generate", json={"date": "2026-11-23", "hour": 19}).json()
    assert snap["version"] == 1 and snap["calling_status"] == "ok"
    assert snap["calling"]["sections"]["setter"]["rows"][0]["name"] == "Ansh Singh"
    assert snap["booking_status"] == "not_configured"

    latest = client.get("/api/reports/latest").json()
    assert latest["id"] == snap["id"]
    day = client.get("/api/reports", params={"day": "2026-11-23"}).json()
    assert [c["label"] for c in day["cycles"]] == ["2 PM", "5 PM", "7 PM", "9 PM"]

    x = client.get(f"/api/reports/{snap['id']}/download?format=xlsx")
    wb = load_workbook(io.BytesIO(x.content))
    ws = wb["7 PM report"]
    values = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    assert "Ansh Singh" in values and 675 in values and 613.8 in values
    csv = client.get(f"/api/reports/{snap['id']}/download?format=csv").text
    assert "TT (in min)" in csv and "Ansh Singh" in csv
    assert client.get("/api/days/2026-11-23/download").status_code == 200
    assert client.get(f"/api/uploads/{latest['calling']['source']['upload_id']}/raw").content == real_csv_bytes

    s2 = client.post("/api/reports/generate", json={"date": "2026-11-23", "hour": 14}).json()
    cmp = client.get("/api/compare", params={"a": s2["id"], "b": snap["id"]}).json()
    assert cmp["from"]["label"].startswith("23 Nov 2 PM")
    assert client.get("/api/timeline", params={"day": "2026-11-23"}).status_code == 200


def test_roster_edit_changes_next_report(client, real_csv_bytes):
    login(client)
    client.post("/api/uploads", files={"file": (REAL_CSV_NAME, real_csv_bytes, "text/csv")})
    roster = client.get("/api/roster").json()
    garima = next(p for p in roster if p["name"] == "Garima Lohani")
    assert client.patch(f"/api/roster/{garima['id']}", json={"role": "setter"}).status_code == 200
    snap = client.post("/api/reports/generate", json={"date": "2026-11-23", "hour": 19}).json()
    assert "Garima Lohani" in [r["name"] for r in snap["calling"]["sections"]["setter"]["rows"]]
    assert client.patch(f"/api/roster/{garima['id']}", json={"role": "boss"}).status_code == 400


def test_viewer_is_read_only(client, real_csv_bytes):
    login(client, "viewer-pass")
    assert client.post("/api/uploads", files={"file": (REAL_CSV_NAME, real_csv_bytes, "text/csv")}).status_code == 403
    assert client.post("/api/reports/generate", json={"date": "2026-11-23", "hour": 19}).status_code == 403
    assert client.get("/api/roster").status_code == 200


def test_rejects_bad_files_and_future_dates(client):
    login(client)
    assert client.post("/api/uploads", files={"file": ("x.xlsx", b"abc", "application/octet-stream")}).status_code == 400
    bad = client.post("/api/uploads", files={"file": ("x.csv", b"User,Total calls\nA,1\n", "text/csv")}).json()
    assert bad["valid"] is False
    assert client.post("/api/reports/generate", json={"date": "2099-01-01", "hour": 14}).status_code == 400
    assert client.post("/api/reports/generate", json={"date": "2026-11-23", "hour": 15}).status_code == 400


def test_ingest_and_cron_need_secrets(client, real_csv_bytes):
    files = {"file": (REAL_CSV_NAME, real_csv_bytes, "text/csv")}
    assert client.post("/api/ingest/csv", files=files).status_code == 401
    r = client.post("/api/ingest/csv", files=files, headers={"X-Ingest-Token": "ingest-token"})
    assert r.status_code == 200 and r.json()["source"] == "api"
    assert client.post("/api/cron/run").status_code == 401
    assert client.post("/api/cron/run", headers={"X-Cron-Secret": "cron-secret"}).status_code == 200


def test_logs_and_frontend(client):
    login(client)
    assert client.get("/").status_code == 200 and "Performance Report" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert isinstance(client.get("/api/logs").json(), list)


def test_vercel_cron_get_with_bearer(client):
    assert client.get("/api/cron/run").status_code == 401
    assert client.get("/api/cron/run", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/cron/run", headers={"Authorization": "Bearer cron-secret"}).status_code == 200


def test_setup_page_instead_of_crash(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "on_vercel", True)
    monkeypatch.setattr(settings, "database_url", "sqlite:///./data/reporting.db")
    r = client.get("/")
    assert r.status_code == 503 and "Neon" in r.text
    assert client.get("/api/status").json()["detail"].startswith("Setup needed")
    assert client.get("/healthz").status_code == 200
