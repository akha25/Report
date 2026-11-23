import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_tmp = tempfile.mkdtemp()
os.environ.update({
    "ENV_FILE": "/nonexistent",
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "SCHEDULER_ENABLED": "false",
    "ADMIN_PASSWORD": "admin-pass",
    "VIEWER_PASSWORD": "viewer-pass",
    "SECRET_KEY": "test-secret",
    "CSV_WAIT_MINUTES": "0",
    "AUTO_RETRY_MINUTES": "0",
    "CRON_SECRET": "cron-secret",
    "INGEST_TOKEN": "ingest-token",
    "HUBSPOT_TOKEN": "",
    "SLACK_WEBHOOK_URL": "",
    "TT_DECIMALS": "1",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.db import Base, engine, session_scope  # noqa: E402
from app.hubspot import HubSpotClient  # noqa: E402
from app.roster import seed_roster  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
FIXTURES = Path(__file__).parent / "fixtures"
REAL_CSV_NAME = "users-table-Monday_November_23_2026_at_06_57_PM.csv"
REPORT_DATE = datetime(2026, 11, 23).date()  # a Monday


def ist(h, m=0, d=23):
    return datetime(2026, 11, d, h, m, tzinfo=IST)


def ms(dt):
    return int(dt.timestamp() * 1000)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_scope() as s:
        seed_roster(s)
    yield


@pytest.fixture
def real_csv_bytes():
    return (FIXTURES / REAL_CSV_NAME).read_bytes()


# --------------------------------------------------------------------------- fake HubSpot
class FakeHubSpot:
    """Behaves like the HubSpot endpoints the app uses, including paging."""

    BOOK, CC = "1048885393", "1133451100"

    def __init__(self):
        self.labels = {"90978306": "Ansh Singh", "80602935": "Yusuf Lari", "80602946": "Shweta Pandey",
                       "81371879": "Vrinda Kapur", "55555555": ""}
        self.owners = [
            {"id": "1001", "userId": 90978306, "email": "anshraj.singh@suraasa.com", "firstName": "Ansh", "lastName": "Singh"},
            {"id": "1002", "userId": 80602935, "email": "yusuf.lari@suraasa.com", "firstName": "Yusuf", "lastName": "Lari"},
            {"id": "1003", "userId": 80602946, "email": "shweta.pandey@suraasa.com", "firstName": "Shweta", "lastName": "Pandey"},
            {"id": "1004", "userId": 81371879, "email": "vrinda.kapur@suraasa.com", "firstName": "Vrinda", "lastName": "Kapur"},
            {"id": "1005", "userId": 77777777, "email": "new.person@suraasa.com", "firstName": "New", "lastName": "Person"},
        ]
        # deal id -> (setter value or None, {stage: entered datetime})
        self.deals = {
            "d1": ("90978306", {self.BOOK: ist(10)}),
            "d2": ("90978306", {self.BOOK: ist(11), self.CC: ist(16)}),
            "d3": ("90978306", {self.BOOK: ist(15)}),
            "d4": ("80602935", {self.BOOK: ist(12)}),
            "d5": ("80602935", {self.BOOK: ist(13), self.CC: ist(13, 30)}),
            "d6": ("80602946", {self.BOOK: ist(9)}),           # contact excluded by list 1724
            "d7": (None, {self.BOOK: ist(12)}),                # unassigned
            "d8": ("81371879", {self.CC: ist(18)}),
            "d9": ("80602946", {self.BOOK: ist(19, 30)}),      # after 7 PM
            "d10": ("90978306", {self.BOOK: ist(10, d=22)}),   # yesterday
            "d11": ("80602935", {self.BOOK: ist(14, 30)}),     # shares contact c4 with d4
            "d12": ("77777777", {self.BOOK: ist(11)}),         # setter not on roster
            "d13": ("55555555", {self.BOOK: ist(11)}),         # label exists but blank, no owner
        }
        self.assoc = {"d1": ["c1"], "d2": ["c2"], "d3": ["c3"], "d4": ["c4"], "d5": ["c5"], "d6": ["c6"],
                      "d7": ["c7"], "d8": ["c8"], "d9": ["c9"], "d10": ["c10"], "d11": ["c4"], "d12": ["c12"],
                      "d13": ["c13"]}
        self.lists = {"c6": ["1724"], "c1": ["999"]}
        self.fail_with: int | None = None
        self.calls = []
        self.page_size = 3

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "simulated failure"})
        path = request.url.path
        if path == "/crm/v3/objects/deals/search":
            body = json.loads(request.content)
            f = body["filterGroups"][0]["filters"]
            prop = f[0]["propertyName"]
            stage = prop.rsplit("_", 1)[1]
            lo = int(next(x["value"] for x in f if x["operator"] == "GTE"))
            hi = int(next(x["value"] for x in f if x["operator"] == "LT"))
            hits = []
            for did, (setter, stages) in sorted(self.deals.items()):
                t = stages.get(stage)
                if t and lo <= ms(t) < hi:
                    hits.append({"id": did, "properties": {"asst_counsellor": setter, "dealname": did}})
            start = int(body.get("after") or 0)
            page = hits[start:start + self.page_size]
            out = {"results": page, "total": len(hits)}
            if start + self.page_size < len(hits):
                out["paging"] = {"next": {"after": str(start + self.page_size)}}
            return httpx.Response(200, json=out)
        if path == "/crm/v4/associations/deals/contacts/batch/read":
            ids = [i["id"] for i in json.loads(request.content)["inputs"]]
            return httpx.Response(200, json={"results": [
                {"from": {"id": d}, "to": [{"toObjectId": c} for c in self.assoc.get(d, [])]}
                for d in ids if self.assoc.get(d)]})
        m = re.match(r"^/crm/v3/lists/records/0-1/(\w+)/memberships$", path)
        if m:
            return httpx.Response(200, json={"results": [{"listId": l} for l in self.lists.get(m.group(1), [])]})
        if path == "/crm/v3/properties/deals/asst_counsellor":
            return httpx.Response(200, json={"options": [{"value": k, "label": v} for k, v in self.labels.items()]})
        if path == "/crm/v3/owners":
            archived = request.url.params.get("archived") == "true"
            return httpx.Response(200, json={"results": [] if archived else self.owners})
        return httpx.Response(404, json={"message": f"unknown path {path}"})

    def factory(self):
        return lambda: HubSpotClient(token="test", base_url="https://api.hubapi.com",
                                     transport=httpx.MockTransport(self.handler), backoff_seconds=0)


@pytest.fixture
def fake_hubspot():
    return FakeHubSpot()
