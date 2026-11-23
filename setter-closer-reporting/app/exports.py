"""Downloads: one snapshot as CSV or Excel, or a whole day (all cycles) as one Excel workbook."""
from __future__ import annotations

import csv
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import settings
from .models import Snapshot
from .timeutil import cycle_label, to_ist

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
TOTAL_FONT = Font(bold=True)
SECTION_FONT = Font(bold=True, size=12)


def calling_rows(snap: Snapshot) -> list[list]:
    out = []
    if snap.calling_status not in ("ok", "stale"):
        return out
    for key in ("setter", "closer", "unclassified"):
        sec = snap.calling["sections"][key]
        for r in sec["rows"]:
            out.append([sec["title"], r["name"], r["calls"], r["tt_minutes"], r["connected"], r["unique"]])
        if sec["rows"]:
            t = sec["totals"]
            out.append([sec["title"], f"Total {sec['title']}", t["calls"], t["tt_minutes"], t["connected"], t["unique"]])
    t = snap.calling["totals"]["setters_and_closers"]
    out.append(["All", "Setters + Closers", t["calls"], t["tt_minutes"], t["connected"], t["unique"]])
    return out


def booking_rows(snap: Snapshot) -> list[list]:
    if snap.booking_status != "ok":
        return []
    rows = [[r["name"], r["bookings"], r["cc"]] for r in snap.booking["rows"]]
    t = snap.booking["totals"]
    rows.append(["Total", t["bookings"], t["cc"]])
    return rows


def snapshot_csv(snap: Snapshot) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Setter & Closer Report till {cycle_label(snap.cycle_hour)}", snap.report_date.isoformat(),
                f"version {snap.version}", f"status {snap.status}"])
    w.writerow([])
    w.writerow(["CALLING INPUTS", snap.calling_status, snap.calling_message])
    w.writerow(["Group", "Setter Name", "Calls", "TT (in min)", "Connected", "Unique Calls"])
    w.writerows(calling_rows(snap))
    w.writerow([])
    w.writerow(["BOOKING INPUTS", snap.booking_status, snap.booking_message])
    w.writerow(["Setter Name", "Bookings", "CC"])
    w.writerows(booking_rows(snap))
    return buf.getvalue().encode("utf-8-sig")


def _write_block(ws, start_row: int, title: str, headers: list[str], rows: list[list], status_note: str = "") -> int:
    ws.cell(row=start_row, column=1, value=title).font = SECTION_FONT
    if status_note:
        ws.cell(row=start_row, column=3, value=status_note).font = Font(italic=True, color="B42318")
    r = start_row + 1
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        r += 1
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=v)
            if isinstance(v, float):
                cell.number_format = "0." + "0" * settings.tt_decimals if settings.tt_decimals else "0"
        if str(row[1] if len(row) > 1 else row[0]).startswith(("Total", "Setters + Closers")) or row[0] == "Total":
            for c in range(1, len(row) + 1):
                ws.cell(row=r, column=c).font = TOTAL_FONT
    return r + 2


def _snapshot_sheet(wb: Workbook, snap: Snapshot, title: str):
    ws = wb.create_sheet(title=title[:31])
    ws["A1"] = f"Setter & Closer Report · till {cycle_label(snap.cycle_hour)} · {snap.report_date:%d %b %Y}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"Version {snap.version} · generated {to_ist(snap.created_at):%d %b %Y %I:%M %p} IST · "
                f"status: {snap.status}")
    row = 4
    note = "" if snap.calling_status == "ok" else f"{snap.calling_status}: {snap.calling_message}"
    row = _write_block(ws, row, "Calling Inputs (sorted by TT)",
                       ["Group", "Setter Name", "Calls", "TT (in min)", "Connected", "Unique Calls"],
                       calling_rows(snap), note)
    note = "" if snap.booking_status == "ok" else f"{snap.booking_status}: {snap.booking_message}"
    _write_block(ws, row, "Booking Inputs (sorted by Bookings)", ["Setter Name", "Bookings", "CC"],
                 booking_rows(snap), note)
    for col, width in zip("ABCDEF", (16, 26, 10, 12, 12, 14)):
        ws.column_dimensions[col].width = width
    return ws


def _source_sheet(wb: Workbook, snaps: list[Snapshot]):
    ws = wb.create_sheet("Sources")
    headers = ["Report", "Version", "Calling status", "FreJun file", "FreJun exported", "Booking status",
               "HubSpot window start", "HubSpot window end", "Booked deals", "CC deals", "Excluded lists"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
    for i, s in enumerate(snaps, 2):
        src = (s.calling or {}).get("source", {})
        meta = (s.booking or {}).get("meta", {})
        vals = [f"{s.report_date} {cycle_label(s.cycle_hour)}", s.version, s.calling_status, src.get("filename"),
                src.get("exported_at"), s.booking_status, meta.get("window_start"), meta.get("window_end"),
                meta.get("booked_deals"), meta.get("cc_deals"), ", ".join(meta.get("excluded_list_ids", []))]
        for c, v in enumerate(vals, 1):
            ws.cell(row=i, column=c, value=v)
    for c in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 22


def snapshot_xlsx(snap: Snapshot) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    _snapshot_sheet(wb, snap, f"{cycle_label(snap.cycle_hour)} report")
    _source_sheet(wb, [snap])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def day_xlsx(snaps: list[Snapshot]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for s in snaps:
        _snapshot_sheet(wb, s, f"{cycle_label(s.cycle_hour)}")
    if not snaps:
        wb.create_sheet("No reports")
    else:
        _source_sheet(wb, snaps)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
