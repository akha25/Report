from datetime import date

import httpx
from sqlalchemy import select

from app import scheduler
from app.config import settings
from app.db import session_scope
from app.delivery import deliver, slack_payload
from app.models import Delivery, Snapshot
from app.reporting import generate_snapshot, store_csv_upload
from app.timeutil import due_cycles, is_working_day, next_cycle
from conftest import REAL_CSV_NAME, REPORT_DATE, ist


def test_working_days_exclude_sunday():
    assert is_working_day(date(2026, 11, 28))  # Saturday
    assert not is_working_day(date(2026, 11, 29))  # Sunday
    assert due_cycles(ist(22, d=29)) == []
    assert next_cycle(ist(21, 30, d=28)) == (date(2026, 11, 30), 14)  # Sat night -> Monday 2 PM
    assert next_cycle(ist(15)) == (REPORT_DATE, 17)
    assert [h for _, h in due_cycles(ist(19, 0))] == [14, 17, 19]


def test_run_cycle_skips_sunday_and_never_duplicates(fake_hubspot):
    assert scheduler.run_cycle(14, date(2026, 11, 29), wait=False) is None
    first = scheduler.run_cycle(14, REPORT_DATE, wait=False, client_factory=fake_hubspot.factory())
    again = scheduler.run_cycle(14, REPORT_DATE, wait=False, client_factory=fake_hubspot.factory())
    assert first and again is None
    with session_scope() as s:
        assert len(s.scalars(select(Snapshot)).all()) == 1


def test_catch_up_generates_only_missing_cycles(fake_hubspot):
    scheduler.run_cycle(14, REPORT_DATE, wait=False, client_factory=fake_hubspot.factory())
    made = scheduler.catch_up(at=ist(19, 30), client_factory=fake_hubspot.factory())
    assert len(made) == 2
    with session_scope() as s:
        hours = sorted(x.cycle_hour for x in s.scalars(select(Snapshot)).all())
        assert hours == [14, 17, 19]
        assert {x.trigger for x in s.scalars(select(Snapshot)).all()} == {"scheduled", "catch-up"}


class FakeSlack:
    def __init__(self, fail=False):
        self.posts, self.fail = [], fail

    def transport(self):
        def handler(request):
            self.posts.append(request.content)
            return httpx.Response(500 if self.fail else 200, text="ok")
        return httpx.MockTransport(handler)


def test_slack_posts_once_per_cycle_then_update_when_fixed(real_csv_bytes, fake_hubspot, monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")
    slack = FakeSlack()
    with session_scope() as s:
        store_csv_upload(s, real_csv_bytes, REAL_CSV_NAME)
        fake_hubspot.fail_with = 503
        v1 = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 1), client_factory=fake_hubspot.factory())
        assert deliver(s, v1, transport=slack.transport()).status == "sent"
        v1_again = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 2), client_factory=fake_hubspot.factory())
        assert deliver(s, v1_again, transport=slack.transport()) is None  # still partial -> no repost
        fake_hubspot.fail_with = None
        v3 = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 12), client_factory=fake_hubspot.factory())
        d = deliver(s, v3, transport=slack.transport())
        assert d.kind == "update" and d.status == "sent"
        v4 = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 20), client_factory=fake_hubspot.factory())
        assert deliver(s, v4, transport=slack.transport()) is None  # complete already posted
        assert deliver(s, v4, force=True, transport=slack.transport()).kind == "correction"
    assert len(slack.posts) == 3
    assert b"UPDATE" in slack.posts[1] and b"CORRECTION" in slack.posts[2]


def test_slack_failure_is_logged_not_raised(real_csv_bytes, fake_hubspot, monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 14, now=ist(14, 1), client_factory=fake_hubspot.factory())
        d = deliver(s, snap, transport=FakeSlack(fail=True).transport())
        assert d.status == "failed"
        # a failed post doesn't count as posted, so the next attempt goes out as a normal report
        assert deliver(s, snap, transport=FakeSlack().transport()).kind == "report"


def test_slack_payload_contents(real_csv_bytes, fake_hubspot):
    with session_scope() as s:
        store_csv_upload(s, real_csv_bytes, REAL_CSV_NAME)
        snap = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 1), client_factory=fake_hubspot.factory())
        text = str(slack_payload(snap))
        assert "till 7 PM" in text and "Ansh Singh" in text and "109.7" in text and "Setter Name" in text
