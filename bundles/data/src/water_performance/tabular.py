"""Read tables by content: find the header row a contract describes, and read values safely.

Parsers never rely on file names or fixed offsets. A contract lists the logical columns it needs
and the header labels each may appear under; the header row is the first row (within a scan
window) that contains every required column. Values that are missing, placeholders or
spreadsheet errors stay missing with a reason, and are never read as zero.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SPREADSHEET_ERRORS = (
    "#N/A",
    "#DIV/0!",
    "#REF!",
    "#VALUE!",
    "#NAME?",
    "#NUM!",
    "#NULL!",
    "#SPILL!",
    "#CALC!",
    "#GETTING_DATA",
)
PLACEHOLDERS = (
    "",
    "-",
    "--",
    "n/a",
    "na",
    "n.a.",
    "x",
    "tbc",
    "tbd",
    "null",
    "none",
    "nil",
    "not applicable",
    ".",
    "*",
    "..",
)


class ContractError(ValueError):
    """The asset does not match the parser contract. Carries evidence for the reject table."""

    def __init__(self, message: str, evidence: dict | None = None):
        super().__init__(message)
        self.evidence = evidence or {}


def norm_label(value) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


@dataclass(frozen=True)
class Column:
    name: str
    labels: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class Header:
    row_index: int  # zero-based index in the scanned rows
    columns: dict[str, int]  # logical name -> zero-based column index
    labels: dict[str, str]  # logical name -> header label as published


def find_header(rows: Iterable[Sequence], columns: Sequence[Column], scan_rows: int = 40) -> Header:
    wanted = {c.name: {norm_label(x) for x in c.labels} for c in columns}
    seen = []
    for i, row in enumerate(rows):
        if i >= scan_rows:
            break
        cells = [norm_label(v) for v in row]
        seen.append([str(v)[:40] for v in row[:20] if v not in (None, "")])
        found: dict[str, int] = {}
        for c in columns:
            idx = next((j for j, label in enumerate(cells) if label in wanted[c.name]), None)
            if idx is not None:
                found[c.name] = idx
        if all(c.name in found for c in columns if c.required):
            return Header(i, found, {k: str(row[v]) for k, v in found.items()})
    required = [c.name for c in columns if c.required]
    raise ContractError(
        f"no row in the first {scan_rows} rows contains every required column {required}",
        {"expected": {c.name: list(c.labels) for c in columns}, "first_rows": seen[:12]},
    )


@dataclass(frozen=True)
class Number:
    value: float | None
    reason: str | None  # None when the value is a number
    raw: str | None


def read_number(raw) -> Number:
    """A published number, or a reason it is not one. Booleans and dates are not numbers."""
    if raw is None:
        return Number(None, "missing", None)
    if isinstance(raw, bool):
        return Number(None, "not_numeric", str(raw))
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
            return Number(None, "not_numeric", str(raw))
        return Number(float(raw), None, repr(raw))
    text = str(raw).strip()
    if text.upper() in SPREADSHEET_ERRORS or (text.startswith("#") and text.endswith(("!", "?", "A"))):
        return Number(None, "spreadsheet_error", text)
    if text.lower() in PLACEHOLDERS:
        return Number(None, "placeholder" if text else "missing", text)
    cleaned = text.replace(",", "").replace("£", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    # A trailing % is dropped, not applied: the published unit column says what the number means.
    cleaned = cleaned[:-1] if cleaned.endswith("%") else cleaned
    try:
        value = float(cleaned)
    except ValueError:
        return Number(None, "not_numeric", text)
    if math.isnan(value) or math.isinf(value):
        return Number(None, "not_numeric", text)
    return Number(-value if negative else value, None, text)


def text(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def csv_rows(path: str):
    """CSV rows, decoded as UTF-8 or, failing that, Windows-1252 (some publishers export £ that way)."""
    import csv
    import io

    with open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    yield from csv.reader(io.StringIO(text, newline=""))


def xlsx_sheets(path: str):
    """(sheet name, row iterator) for each worksheet, reading cached values only."""
    from .assets import open_workbook

    wb = open_workbook(path)
    try:
        for ws in wb.worksheets:
            yield ws.title, ws.iter_rows(values_only=True)
    finally:
        wb.close()


def is_time_unit(unit: str | None) -> bool:
    """Units published as Excel times: 'hh:mm:ss', '... (hh:mm:ss)' or exactly 'Time'."""
    u = (unit or "").strip().lower()
    return "hh:mm" in u or u == "time"


def to_minutes(raw) -> float | None:
    """Excel time values as minutes: a time of day, a timedelta, an h:mm:ss string or a fraction of a day."""
    if isinstance(raw, dt.datetime):
        return to_minutes(raw.time()) + (raw - dt.datetime(1899, 12, 30)).days * 1440
    if isinstance(raw, dt.time):
        return raw.hour * 60 + raw.minute + (raw.second + raw.microsecond / 1e6) / 60
    if isinstance(raw, dt.timedelta):
        return raw.total_seconds() / 60
    if isinstance(raw, str) and (m := re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)\s*", raw)):
        return int(m.group(1)) * 60 + int(m.group(2)) + float(m.group(3)) / 60
    n = read_number(raw)
    return None if n.value is None else n.value * 1440


def read_value(raw, unit: str | None) -> tuple[float | None, str | None, str | None]:
    """(value, reason, unit as loaded). Time units become minutes and the loaded unit says so."""
    if not is_time_unit(unit):
        n = read_number(raw)
        return n.value, n.reason, unit
    if isinstance(raw, str):
        text = raw.strip()
        if text.upper() in SPREADSHEET_ERRORS:
            return None, "spreadsheet_error", None
        if text.lower() in PLACEHOLDERS:
            return None, "placeholder" if text else "missing", None
    if raw is None:
        return None, "missing", None
    minutes = to_minutes(raw)
    return (minutes, None, "minutes") if minutes is not None else (None, "not_numeric", None)
