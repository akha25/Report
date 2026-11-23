from datetime import datetime

import pytest

from app.csv_parser import export_time_from_filename, parse_duration, parse_frejun_csv, seconds_to_minutes
from conftest import IST, REAL_CSV_NAME

NOW = datetime(2026, 11, 23, 19, 5, tzinfo=IST)
SETTERS = ["Ansh Singh", "Yusuf Lari", "Shweta Pandey", "Prateekhya Pal", "Nikhil Singh", "Atulya Goswami",
           "Harsh Vindra Pandey", "Vrinda Kapur", "Amit Kumar Rai"]


@pytest.mark.parametrize("raw,secs,fmt", [
    ("360", 360, "seconds"), ("1200", 1200, "seconds"), ("01:30:00", 5400, "hh:mm:ss"),
    ("1h 43m 45s", 6225, "1h 2m 3s"), ("37m 4s", 2224, "1h 2m 3s"), ("12s", 12, "1h 2m 3s"),
    ("2h", 7200, "1h 2m 3s"), ("4:05", 245, "mm:ss"), ("0", 0, "seconds"),
    ("", None, "blank"), ("-", None, "blank"), ("abc", None, "unreadable"), ("-5", None, "negative"),
])
def test_parse_duration(raw, secs, fmt):
    assert parse_duration(raw) == (secs, fmt)


def test_spec_examples_in_minutes():
    assert seconds_to_minutes(parse_duration("360")[0]) == 6.0
    assert seconds_to_minutes(parse_duration("1,200".replace(",", ""))[0]) == 20.0
    assert seconds_to_minutes(parse_duration("01:30:00")[0]) == 90.0
    assert seconds_to_minutes(6579) == 109.7  # Ansh, 1h 49m 39s
    assert seconds_to_minutes(90, 0) == 2.0  # half rounds up


def test_real_frejun_export(real_csv_bytes):
    r = parse_frejun_csv(real_csv_bytes, REAL_CSV_NAME, NOW)
    assert r.valid
    assert r.column_map == {"name": "User", "email": "Email", "calls": "Total calls", "tt": "Total duration",
                            "connected": "Calls Connected", "unique": "Unique Calls"}
    assert r.exported_at == datetime(2026, 11, 23, 18, 57, tzinfo=IST) and r.exported_at_source == "filename"
    assert len(r.rows) == 41
    setters = [x for x in r.rows if x["name"] in SETTERS]
    # Matches the manual 7 PM report on 23 Nov exactly for calls / connected / unique.
    assert sum(x["calls"] for x in setters) == 675
    assert sum(x["connected"] for x in setters) == 248
    assert sum(x["unique"] for x in setters) == 483
    assert sum(x["tt_seconds"] for x in setters) == 36828  # 613.8 minutes
    ansh = next(x for x in r.rows if x["name"] == "Ansh Singh")
    assert ansh["tt_seconds"] == 6579 and ansh["email"] == "anshraj.singh@suraasa.com"
    assert not [i for i in r.issues if i["level"] in ("error", "warning")]


def test_average_talktime_is_never_used_as_tt(real_csv_bytes):
    r = parse_frejun_csv(real_csv_bytes, REAL_CSV_NAME, NOW)
    yusuf = next(x for x in r.rows if x["name"] == "Yusuf Lari")
    assert yusuf["tt_seconds"] == 6225  # Total duration 1h 43m 45s, not Average Talktime 3m 8s


def test_minutes_header_reads_plain_numbers_as_minutes():
    raw = b"Setter Name,Calls,TT (in min),Connected,Unique Calls\nA,10,12.5,4,8\n"
    r = parse_frejun_csv(raw, "x.csv", NOW)
    assert r.valid and r.rows[0]["tt_seconds"] == 750 and r.rows[0]["tt_minutes"] == 12.5


def test_missing_column_rejected():
    r = parse_frejun_csv(b"User,Email,Total calls,Calls Connected\nA,a@x.com,1,1\n", "x.csv", NOW)
    assert not r.valid
    msgs = " ".join(i["message"] for i in r.issues if i["level"] == "error")
    assert "Talk Time" in msgs and "Unique Calls" in msgs


def test_duplicate_identical_counted_once_and_conflict_rejected():
    head = b"User,Email,Total calls,Total duration,Calls Connected,Unique Calls\n"
    same = head + b"A,a@x.com,5,1m,2,4\nA,a@x.com,5,1m,2,4\n"
    r = parse_frejun_csv(same, "x.csv", NOW)
    assert r.valid and len(r.rows) == 1 and any(i["code"] == "duplicate_identical" for i in r.issues)
    clash = head + b"A,a@x.com,5,1m,2,4\nA,a@x.com,9,3m,2,4\n"
    r = parse_frejun_csv(clash, "x.csv", NOW)
    assert not r.valid and any(i["code"] == "duplicate_conflict" for i in r.issues)


def test_two_people_same_name_different_email_not_combined():
    raw = (b"User,Email,Total calls,Total duration,Calls Connected,Unique Calls\n"
           b"Shweta Singh,shweta.singh@x.com,5,1m,2,4\nShweta Singh,shweta.s2@x.com,9,3m,2,4\n")
    r = parse_frejun_csv(raw, "x.csv", NOW)
    assert r.valid and len(r.rows) == 2


def test_blank_and_bad_values_flagged_not_zeroed():
    raw = b"User,Email,Total calls,Total duration,Calls Connected,Unique Calls\nA,a@x.com,5,,2,4\nB,b@x.com,x,1m,9,4\n"
    r = parse_frejun_csv(raw, "x.csv", NOW)
    a, b = r.rows
    assert a["tt_seconds"] is None and a["flags"]
    assert b["calls"] is None
    codes = {i["code"] for i in r.issues}
    assert {"bad_tt", "bad_calls"} <= codes


def test_sanity_warnings():
    raw = b"User,Email,Total calls,Total duration,Calls Connected,Unique Calls\nA,a@x.com,5,1m,9,7\n"
    codes = {i["code"] for i in parse_frejun_csv(raw, "x.csv", NOW).issues}
    assert {"connected_gt_calls", "unique_gt_calls"} <= codes


def test_export_time_from_filename_and_fallback():
    assert export_time_from_filename("users-table-Saturday_November_28_2026_at_02_03_PM.csv") == \
        datetime(2026, 11, 28, 14, 3, tzinfo=IST)
    r = parse_frejun_csv(b"User,Total calls,Total duration,Calls Connected,Unique Calls\nA,1,1m,1,1\n", "renamed.csv", NOW)
    assert r.exported_at == NOW and r.exported_at_source == "upload_time"
    assert any(i["code"] == "export_time_unknown" for i in r.issues)
    assert any(i["code"] == "no_email_column" for i in r.issues)


def test_empty_and_header_only():
    assert not parse_frejun_csv(b"", "x.csv", NOW).valid
    assert not parse_frejun_csv(b"User,Email,Total calls,Total duration,Calls Connected,Unique Calls\n", "x.csv", NOW).valid
