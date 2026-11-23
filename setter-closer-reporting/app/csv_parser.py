"""FreJun CSV ingestion.

Inspected format (users-table export, Nov 2026):
    "User","Email","Total calls","Total duration","Calls Connected","Average Talktime",
    "Unique Calls","Calls Greater than 5 mins","Calls less than 1 mins","Incoming"

* "Total duration" is the user's total talk time ("1h 43m 45s"). It equals Average Talktime x
  Calls Connected, so it is talk time on connected calls, not ring/dial time. It is the TT source.
* "Average Talktime" is never used for TT.
* Columns are matched by header name (never by position); unknown layouts are rejected with a
  clear message instead of being guessed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .config import settings
from .timeutil import TZ, to_ist

# Accepted header names per field, after normalisation (lowercase, punctuation -> space).
HEADER_SYNONYMS: dict[str, list[str]] = {
    "name": ["user", "user name", "username", "agent", "agent name", "name", "setter name",
             "employee", "employee name", "caller", "caller name"],
    "email": ["email", "user email", "email id", "agent email", "email address"],
    "calls": ["total calls", "calls", "number of calls", "no of calls", "call count", "total dials"],
    "tt": ["total duration", "talk time", "total talk time", "talktime", "total talktime", "tt",
           "tt in min", "talk time in min", "talk time min", "talk time minutes"],
    "connected": ["calls connected", "connected", "connected calls", "answered", "answered calls"],
    "unique": ["unique calls", "unique", "unique dials", "unique numbers", "unique contacts"],
}
REQUIRED = ["name", "calls", "tt", "connected", "unique"]
FIELD_LABELS = {"name": "Setter Name", "email": "Email", "calls": "Calls", "tt": "Talk Time",
                "connected": "Connected", "unique": "Unique Calls"}

_UNIT_RE = re.compile(
    r"^\s*(?:(?P<h>\d+(?:\.\d+)?)\s*h(?:r|rs|our|ours)?)?\s*"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*m(?:in|ins|inute|inutes)?)?\s*"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s(?:ec|ecs|econd|econds)?)?\s*$",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(r"^\s*(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?\s*$")
_NUM_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*$")
_FILENAME_TS_RE = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"_(?P<day>\d{1,2})_(?P<year>\d{4})_at_(?P<hour>\d{1,2})_(?P<minute>\d{2})_(?P<ampm>AM|PM)",
    re.IGNORECASE,
)


def normalise_header(h: str) -> str:
    h = h.replace("﻿", "").strip().lower()
    h = re.sub(r"[^a-z0-9]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def normalise_name(n: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (n or "").lower()).strip()


def issue(level: str, code: str, message: str, line: int | None = None, field_name: str | None = None) -> dict:
    d = {"level": level, "code": code, "message": message}
    if line is not None:
        d["line"] = line
    if field_name:
        d["field"] = field_name
    return d


def parse_duration(value: str | None, plain_unit: str = "seconds") -> tuple[float | None, str]:
    """Return (seconds, format_name). seconds is None for blank or unreadable values."""
    if value is None:
        return None, "blank"
    v = str(value).strip()
    if v in {"", "-", "--", "NA", "N/A", "na", "n/a", "null", "None"}:
        return None, "blank"
    if _NUM_RE.match(v):
        num = float(v)
        if num < 0:
            return None, "negative"
        return (num * 60.0, "minutes") if plain_unit == "minutes" else (num, "seconds")
    m = _CLOCK_RE.match(v)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c is not None:
            return int(a) * 3600 + int(b) * 60 + float(c), "hh:mm:ss"
        return int(a) * 60 + int(b), "mm:ss"
    m = _UNIT_RE.match(v)
    if m and any(m.group(k) for k in ("h", "m", "s")):
        secs = float(m.group("h") or 0) * 3600 + float(m.group("m") or 0) * 60 + float(m.group("s") or 0)
        return secs, "1h 2m 3s"
    return None, "unreadable"


def seconds_to_minutes(seconds: float | None, decimals: int | None = None) -> float | None:
    if seconds is None:
        return None
    decimals = settings.tt_decimals if decimals is None else decimals
    q = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return float((Decimal(str(seconds)) / Decimal(60)).quantize(q, rounding=ROUND_HALF_UP))


def parse_int(value: str | None) -> tuple[int | None, str | None]:
    if value is None:
        return None, "blank"
    v = str(value).strip().replace(",", "")
    if v in {"", "-", "--", "NA", "N/A", "na", "n/a", "null", "None"}:
        return None, "blank"
    if not _NUM_RE.match(v):
        return None, "unreadable"
    f = float(v)
    if f != int(f):
        return None, "not a whole number"
    if f < 0:
        return None, "negative"
    return int(f), None


def export_time_from_filename(filename: str) -> datetime | None:
    m = _FILENAME_TS_RE.search(filename or "")
    if not m:
        return None
    try:
        dt = datetime.strptime(
            f"{m['month']} {m['day']} {m['year']} {m['hour']}:{m['minute']} {m['ampm'].upper()}",
            "%B %d %Y %I:%M %p",
        )
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ)


@dataclass
class ParseResult:
    valid: bool
    filename: str
    sha256: str
    column_map: dict = field(default_factory=dict)
    tt_format: str = ""
    rows: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    exported_at: datetime | None = None
    exported_at_source: str = ""

    @property
    def report_date(self):
        return self.exported_at.date() if self.exported_at else None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def map_columns(headers: list[str]) -> tuple[dict[str, str], list[dict]]:
    issues: list[dict] = []
    normalised = {h: normalise_header(h) for h in headers}
    mapping: dict[str, str] = {}
    for fld, names in HEADER_SYNONYMS.items():
        hits = [h for h, n in normalised.items() if n in names]
        if len(hits) > 1:
            issues.append(issue("error", "ambiguous_column",
                                f"More than one column could be {FIELD_LABELS[fld]}: {', '.join(hits)}"))
        elif hits:
            mapping[fld] = hits[0]
    for fld in REQUIRED:
        if fld not in mapping:
            issues.append(issue("error", "missing_column",
                                f"Missing a {FIELD_LABELS[fld]} column. Accepted headers: "
                                + ", ".join(f'"{n}"' for n in HEADER_SYNONYMS[fld][:5])))
    if "email" not in mapping:
        issues.append(issue("warning", "no_email_column",
                            "No Email column. People will be matched to the roster by name only."))
    ignored = [h for h in headers if h not in mapping.values() and h.strip()]
    if ignored:
        issues.append(issue("info", "ignored_columns", "Columns not used: " + ", ".join(ignored)))
    return mapping, issues


def parse_frejun_csv(raw: bytes, filename: str, uploaded_at: datetime,
                     exported_at_override: datetime | None = None) -> ParseResult:
    sha = hashlib.sha256(raw).hexdigest()
    result = ParseResult(valid=False, filename=filename, sha256=sha)

    if exported_at_override:
        result.exported_at, result.exported_at_source = to_ist(exported_at_override), "manual"
    elif (ts := export_time_from_filename(filename)) is not None:
        result.exported_at, result.exported_at_source = ts, "filename"
    else:
        result.exported_at, result.exported_at_source = to_ist(uploaded_at), "upload_time"
        result.issues.append(issue("warning", "export_time_unknown",
                                   "The export time isn't in the file name, so the upload time is used. "
                                   "Keep FreJun's original file name to avoid this."))

    if not raw.strip():
        result.issues.append(issue("error", "empty_file", "The file is empty."))
        return result

    text = _decode(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    all_rows = [r for r in reader]
    if not all_rows:
        result.issues.append(issue("error", "empty_file", "The file has no rows."))
        return result

    headers = [h.replace("﻿", "").strip() for h in all_rows[0]]
    mapping, map_issues = map_columns(headers)
    result.column_map = mapping
    result.issues.extend(map_issues)
    if any(i["level"] == "error" for i in map_issues):
        return result

    idx = {fld: headers.index(col) for fld, col in mapping.items()}
    plain_unit = "minutes" if "min" in normalise_header(mapping["tt"]).split() or \
        normalise_header(mapping["tt"]).endswith("min") else settings.plain_number_tt_unit

    formats_seen: set[str] = set()
    parsed: list[dict] = []
    for line_no, r in enumerate(all_rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) < len(headers):
            r = r + [""] * (len(headers) - len(r))

        def cell(fld: str) -> str:
            return r[idx[fld]].strip() if fld in idx else ""

        name = cell("name")
        email = cell("email").lower()
        if not name and not email:
            result.issues.append(issue("warning", "row_without_person",
                                       "Row has no name or email and was skipped.", line_no))
            continue
        if not name:
            result.issues.append(issue("warning", "missing_name",
                                       f"No name on this row; showing the email {email}.", line_no, "name"))
            name = email

        row = {"line": line_no, "name": name, "email": email or None, "flags": [],
               "source": {mapping[f]: cell(f) for f in mapping}}
        for fld in ("calls", "connected", "unique"):
            val, err = parse_int(cell(fld))
            row[fld] = val
            if err:
                row["flags"].append(f"{FIELD_LABELS[fld]} {err}")
                result.issues.append(issue("warning", f"bad_{fld}",
                                           f"{name}: {FIELD_LABELS[fld]} is {err} ('{cell(fld)}').",
                                           line_no, fld))
        secs, fmt = parse_duration(cell("tt"), plain_unit)
        if secs is None:
            row["flags"].append(f"Talk time {fmt}")
            result.issues.append(issue("warning", "bad_tt",
                                       f"{name}: talk time is {fmt} ('{cell('tt')}'). Shown as missing, not zero.",
                                       line_no, "tt"))
        else:
            if not (fmt == "seconds" and secs == 0):
                formats_seen.add(fmt)
        row["tt_seconds"] = secs
        row["tt_minutes_exact"] = None if secs is None else secs / 60.0
        row["tt_minutes"] = seconds_to_minutes(secs)

        c, cn, u = row["calls"], row["connected"], row["unique"]
        if c is not None and cn is not None and cn > c:
            result.issues.append(issue("warning", "connected_gt_calls",
                                       f"{name}: Connected ({cn}) is more than Calls ({c}).", line_no))
        if c is not None and u is not None and u > c:
            result.issues.append(issue("warning", "unique_gt_calls",
                                       f"{name}: Unique Calls ({u}) is more than Calls ({c}).", line_no))
        if secs and cn == 0:
            result.issues.append(issue("warning", "tt_without_connected",
                                       f"{name}: has talk time but 0 connected calls.", line_no))
        parsed.append(row)

    if not parsed:
        result.issues.append(issue("error", "no_data_rows", "The file has headers but no data rows."))
        return result

    if "mm:ss" in formats_seen:
        result.issues.append(issue("warning", "tt_mmss_assumed",
                                   "Some talk times look like '12:34'; these were read as minutes:seconds."))
    if "seconds" in formats_seen and plain_unit == "seconds":
        result.issues.append(issue("info", "tt_plain_seconds",
                                   "Plain-number talk times were read as seconds."))
    result.tt_format = ", ".join(sorted(formats_seen)) or "1h 2m 3s"

    # Duplicate detection: same person twice. Identical rows are dropped; conflicting rows are an
    # error (the export should have one row per user, so we refuse to guess which one is right).
    seen: dict[str, dict] = {}
    unique_rows: list[dict] = []
    for row in parsed:
        key = row["email"] or "name:" + normalise_name(row["name"])
        metrics = (row["calls"], row["tt_seconds"], row["connected"], row["unique"])
        if key in seen:
            first = seen[key]
            first_metrics = (first["calls"], first["tt_seconds"], first["connected"], first["unique"])
            if metrics == first_metrics:
                result.issues.append(issue("warning", "duplicate_identical",
                                           f"{row['name']} appears twice with identical numbers "
                                           f"(lines {first['line']} and {row['line']}); counted once.", row["line"]))
                continue
            result.issues.append(issue("error", "duplicate_conflict",
                                       f"{row['name']} appears twice with different numbers (lines {first['line']} "
                                       f"and {row['line']}). Re-export from FreJun; the rows were not combined.",
                                       row["line"]))
        seen[key] = row
        unique_rows.append(row)

    result.rows = unique_rows
    result.valid = not any(i["level"] == "error" for i in result.issues)
    return result
