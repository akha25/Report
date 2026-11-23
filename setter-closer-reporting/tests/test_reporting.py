from sqlalchemy import select

from app.db import session_scope
from app.models import RosterMember, Snapshot
from app.reporting import compare_snapshots, day_timeline, generate_snapshot, store_csv_upload
from conftest import IST, REAL_CSV_NAME, REPORT_DATE, ist

HEAD = b"User,Email,Total calls,Total duration,Calls Connected,Unique Calls\n"


def upload(s, raw, name=REAL_CSV_NAME, exported=None):
    up, _ = store_csv_upload(s, raw, name, "tester", exported_at_override=exported)
    return up


def test_calling_sorted_by_tt_then_calls_and_split_by_role(real_csv_bytes, fake_hubspot):
    with session_scope() as s:
        upload(s, real_csv_bytes)
        snap = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 2), client_factory=fake_hubspot.factory())
        setters = snap.calling["sections"]["setter"]["rows"]
        assert [r["name"] for r in setters] == ["Ansh Singh", "Yusuf Lari", "Shweta Pandey", "Prateekhya Pal",
                                                "Nikhil Singh", "Atulya Goswami", "Harsh Vindra Pandey",
                                                "Vrinda Kapur", "Amit Kumar Rai"]
        t = snap.calling["sections"]["setter"]["totals"]
        assert (t["calls"], t["connected"], t["unique"], t["tt_minutes"]) == (675, 248, 483, 613.8)
        closers = [r["name"] for r in snap.calling["sections"]["closer"]["rows"]]
        assert closers[0] == "Garima Lohani" and "Muskan Dua" in closers and "Priyo Barik" in closers
        # zero-activity people with no role are hidden, not reported
        assert snap.calling["sections"]["unclassified"]["rows"] == []
        assert "Manish Pandey" in snap.calling["hidden"]["no_activity_role_not_set"]
        assert snap.calling_status == "ok" and snap.status == "complete"


def test_tie_on_tt_breaks_on_calls():
    raw = HEAD + b"Ansh Singh,anshraj.singh@suraasa.com,10,5m,1,1\nYusuf Lari,yusuf.lari@suraasa.com,30,5m,1,1\n" \
                 b"Shweta Pandey,shweta.pandey@suraasa.com,99,4m 59s,1,1\n"
    with session_scope() as s:
        upload(s, raw, "users-table-Monday_November_23_2026_at_01_55_PM.csv")
        snap = generate_snapshot(s, REPORT_DATE, 14, now=ist(14, 1))
        assert [r["name"] for r in snap.calling["sections"]["setter"]["rows"]] == ["Yusuf Lari", "Ansh Singh", "Shweta Pandey"]


def test_unknown_active_person_shown_as_role_not_set():
    raw = HEAD + b"Brand New,brand.new@suraasa.com,12,10m,3,9\n"
    with session_scope() as s:
        upload(s, raw, "users-table-Monday_November_23_2026_at_01_55_PM.csv")
        snap = generate_snapshot(s, REPORT_DATE, 14, now=ist(14, 1))
        rows = snap.calling["sections"]["unclassified"]["rows"]
        assert [r["name"] for r in rows] == ["Brand New"]
        assert s.scalar(select(RosterMember).where(RosterMember.email == "brand.new@suraasa.com")).role == "unclassified"


def test_booking_metrics_match_report_logic(fake_hubspot):
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 19, now=ist(20), client_factory=fake_hubspot.factory())
        b = snap.booking
        rows = {r["name"]: r for r in b["rows"]}
        # Ansh: d1,d2,d3 -> 3 contacts; CC d2 (entered 4 PM)
        assert (rows["Ansh Singh"]["bookings"], rows["Ansh Singh"]["cc"]) == (3, 1)
        # Yusuf: d4 + d11 share contact c4 (distinct contacts) + d5 -> 2; CC d5
        assert (rows["Yusuf Lari"]["bookings"], rows["Yusuf Lari"]["cc"]) == (2, 1)
        # Shweta: d6's contact is in excluded list 1724; d9 is after 7 PM -> 0 bookings
        assert rows["Shweta Pandey"]["bookings"] == 0
        assert rows["Vrinda Kapur"]["cc"] == 1
        assert rows["Unassigned"]["bookings"] == 1 and b["rows"][-1]["name"] == "Unassigned"
        assert rows["HubSpot user 55555555"]["name_found"] is False
        assert rows["New Person"]["role"] == "unclassified"  # name from HubSpot owners, not invented
        # rostered setters with nothing in HubSpot show 0 (flagged) rather than disappearing
        assert rows["Nikhil Singh"]["zero_filled"] and rows["Nikhil Singh"]["bookings"] == 0
        # sorted by bookings desc, then CC desc; Unassigned last
        named = [r for r in b["rows"] if r["name"] != "Unassigned"]
        assert [(r["bookings"], r["cc"]) for r in named] == sorted([(r["bookings"], r["cc"]) for r in named], reverse=True)
        assert b["rows"][0]["name"] == "Ansh Singh"
        assert b["totals"] == {"bookings": 8, "cc": 3, "bookings_row_sum": 8, "cc_row_sum": 3}
        assert b["meta"]["excluded_contacts"] == {"c6": ["1724"]}
        assert rows["Ansh Singh"]["booking_deal_ids"] == ["d1", "d2", "d3"]  # traceable
        # HubSpot IDs learned onto the roster for future matching
        assert s.scalar(select(RosterMember).where(RosterMember.name == "Ansh Singh")).hubspot_owner_id == "90978306"


def test_booking_window_ends_at_cycle_time(fake_hubspot):
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 14, now=ist(20), client_factory=fake_hubspot.factory())
        rows = {r["name"]: r for r in snap.booking["rows"]}
        assert rows["Ansh Singh"]["bookings"] == 2  # d3 entered at 3 PM is not in the 2 PM report
        assert rows["Ansh Singh"]["cc"] == 0
        assert snap.booking["meta"]["window_end"].startswith("2026-11-23T14:00:00")


def test_hubspot_failure_keeps_calling_and_invents_nothing(real_csv_bytes, fake_hubspot):
    fake_hubspot.fail_with = 503
    with session_scope() as s:
        upload(s, real_csv_bytes)
        snap = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 1), client_factory=fake_hubspot.factory())
        assert snap.status == "partial" and snap.booking_status == "failed"
        assert snap.booking == {} and "503" in snap.booking_message
        assert snap.calling_status == "ok" and snap.calling["sections"]["setter"]["totals"]["calls"] == 675
        assert len([c for c in fake_hubspot.calls if c[1].endswith("/search")]) == 4  # retried


def test_auth_error_is_not_retried(fake_hubspot):
    fake_hubspot.fail_with = 401
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 1), client_factory=fake_hubspot.factory())
        assert snap.booking_status == "failed" and "scopes" in snap.booking_message
        assert len(fake_hubspot.calls) == 1


def test_missing_csv_marks_calling_missing(fake_hubspot):
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 14, now=ist(14, 1), client_factory=fake_hubspot.factory())
        assert snap.calling_status == "missing" and snap.calling == {}
        assert snap.booking_status == "ok" and snap.status == "partial"


def test_hubspot_not_configured():
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 14, now=ist(14, 1))
        assert snap.booking_status == "not_configured"


def test_csv_selection_and_staleness(real_csv_bytes):
    early = HEAD + b"Ansh Singh,anshraj.singh@suraasa.com,10,5m,1,1\n"
    with session_scope() as s:
        upload(s, early, "users-table-Monday_November_23_2026_at_01_50_PM.csv")
        upload(s, real_csv_bytes)  # 6:57 PM export
        two = generate_snapshot(s, REPORT_DATE, 14, now=ist(20))
        assert two.calling["source"]["filename"].endswith("01_50_PM.csv") and two.calling_status == "ok"
        five = generate_snapshot(s, REPORT_DATE, 17, now=ist(20))
        assert five.calling_status == "stale"  # nothing new since the 2 PM report
        seven = generate_snapshot(s, REPORT_DATE, 19, now=ist(20))
        assert seven.calling["source"]["filename"] == REAL_CSV_NAME and seven.calling_status == "ok"


def test_previous_day_csv_is_never_used(real_csv_bytes):
    with session_scope() as s:
        upload(s, real_csv_bytes)
        snap = generate_snapshot(s, REPORT_DATE.replace(day=24), 14, now=ist(14, 5, d=24))
        assert snap.calling_status == "missing"


def test_rerun_creates_new_version_and_keeps_old(real_csv_bytes, fake_hubspot):
    with session_scope() as s:
        v1 = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 1), client_factory=fake_hubspot.factory())
        v1_id, v1_calling = v1.id, v1.calling_status
        upload(s, real_csv_bytes)
        v2 = generate_snapshot(s, REPORT_DATE, 19, now=ist(19, 10), client_factory=fake_hubspot.factory())
        assert (v1.version, v2.version) == (1, 2)
    with session_scope() as s:
        old = s.get(Snapshot, v1_id)
        assert old.calling_status == v1_calling == "missing"  # untouched
        assert len(s.scalars(select(Snapshot)).all()) == 2


def test_compare_and_timeline(real_csv_bytes, fake_hubspot):
    early = HEAD + b"Ansh Singh,anshraj.singh@suraasa.com,10,50m,5,8\n"
    with session_scope() as s:
        upload(s, early, "users-table-Monday_November_23_2026_at_01_58_PM.csv")
        upload(s, real_csv_bytes)
        a = generate_snapshot(s, REPORT_DATE, 14, now=ist(20), client_factory=fake_hubspot.factory())
        b = generate_snapshot(s, REPORT_DATE, 19, now=ist(20), client_factory=fake_hubspot.factory())
        cmp = compare_snapshots(a, b)
        ansh = next(r for r in cmp["calling"] if r["name"] == "Ansh Singh")
        assert ansh["calls"]["delta"] == 58 and ansh["tt_minutes"]["delta"] == 59.7
        ab = next(r for r in cmp["booking"] if r["name"] == "Ansh Singh")
        assert (ab["bookings"]["from"], ab["bookings"]["to"], ab["bookings"]["delta"]) == (2, 3, 1)
        tl = day_timeline(s, REPORT_DATE)
        p = next(x for x in tl["people"] if x["name"] == "Ansh Singh")
        assert p["tt"] == {14: 50.0, 19: 109.7} and p["bookings"] == {14: 2, 19: 3}


def test_contact_under_two_setters_counts_once_in_total(fake_hubspot):
    fake_hubspot.deals["d14"] = ("80602946", {fake_hubspot.BOOK: ist(12)})
    fake_hubspot.assoc["d14"] = ["c1"]  # c1 already booked by Ansh
    with session_scope() as s:
        snap = generate_snapshot(s, REPORT_DATE, 19, now=ist(20), client_factory=fake_hubspot.factory())
        t = snap.booking["totals"]
        assert t["bookings_row_sum"] == t["bookings"] + 1
        assert {r["name"]: r["bookings"] for r in snap.booking["rows"]}["Shweta Pandey"] == 1


def test_compare_matches_zero_filled_setter_to_later_hubspot_row(fake_hubspot):
    with session_scope() as s:
        a = generate_snapshot(s, REPORT_DATE, 17, now=ist(20), client_factory=fake_hubspot.factory())
        b = generate_snapshot(s, REPORT_DATE, 19, now=ist(20), client_factory=fake_hubspot.factory())
        vr = [r for r in compare_snapshots(a, b)["booking"] if r["name"] == "Vrinda Kapur"]
        assert len(vr) == 1 and vr[0]["cc"] == {"from": 0, "to": 1, "delta": 1}
