"""Company Annual Performance Report workbooks laid out on Ofwat's APR pro forma template.

Each table sheet has a header block (column headings, possibly over several rows with parent
headings spanning their children), a "RAG 4 reference" column, and one row per table line whose
RAG reference looks like `1A.4`. Units and decimal places are either columns or header rows,
depending on the table. Many sheets also carry an "Ofwat Bon Numbers" grid to the right that
mirrors the data grid with each cell's item reference.

The contract finds every one of these by content. Each value cell becomes one record shaped like a
Stream APR record (RAG reference, line description, attribute, unit, value), with the attribute
built the way Stream builds it: parent headings joined by spaces, then " - " and the column
heading. Blank cells are not values; any other cell that is not a number is rejected with its cell
address. Tables keyed by debt instrument identifiers are never read.
"""

from __future__ import annotations

import re
from collections import Counter

from . import periods, tabular
from .assets import open_workbook
from .parsers import (
    ParseResult,
    _provenance,
    _quarantine_conflicts,
    _reject,
    attribute_financial_year,
    normalise_attribute,
)
from .registry import Asset, Company, company_alias_index
from .tabular import ContractError, norm_label

CONTRACT = "company_apr_v1"
TABLE_REF = re.compile(r"^\d{1,2}[A-Z]{1,2}\.\d{1,3}[a-z]?$")
RAG_LABELS = {"rag_4_reference", "rag_reference", "rag_4_ref"}
LINE_LABELS = {"line_description"}
UNIT_LABELS = {"units", "unit"}
DP_LABELS = {"dps", "dp", "decimal_places"}
BON_TITLE = "ofwat_bon_numbers"
BON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
YEAR_END = re.compile(r"(?:ended|ending|to|at)\s+31\s+March\s+(20\d\d)", re.IGNORECASE)
# Debt-instrument registers are never ingested (sources.md, COMPANY_APR).
RESTRICTED = re.compile(r"instrument identifier|\bISIN\b", re.IGNORECASE)
HEADER_SCAN = 40
TITLE_COMPANY_ROWS = 3
SEE_HEADING = re.compile(r"see column heading", re.IGNORECASE)


def _clean(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def _cell(row, c):
    return row[c] if c < len(row) else None


def _col_letter(c: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(c + 1)


def _find_label(rows, labels, before: int | None = None, start: int = 0) -> tuple[int, int] | None:
    for i, row in enumerate(rows[:HEADER_SCAN]):
        if i < start:
            continue
        for j, v in enumerate(row):
            if (before is None or j < before) and norm_label(v) in labels:
                return i, j
    return None


def _paths(rows, header_rows, cols) -> dict[int, list[str]]:
    """Heading path per column. A blank cell takes the heading to its left only when a lower
    header row has a heading in that column, which is how a merged parent heading reads."""
    out = {c: [] for c in cols}
    texts = {(i, c): _clean(_cell(rows[i], c)) for i in header_rows for c in cols}
    for k, i in enumerate(header_rows):
        lower = header_rows[k + 1 :]
        last = None
        for c in cols:
            t = texts[(i, c)]
            if t is not None:
                last = t
            elif last is not None and any(texts[(j, c)] is not None for j in lower):
                t = last
            if t is not None and (not out[c] or out[c][-1] != t):
                out[c].append(t)
    return out


def _attribute(path: list[str]) -> str | None:
    if not path:
        return None
    if len(path) == 1:
        return path[0]
    return f"{' '.join(path[:-1])} - {path[-1]}"


def _table(rows: list[list]) -> dict | None:
    """The layout of one table sheet, or None when the sheet is not an APR table."""
    rag = _find_label(rows, RAG_LABELS)
    if rag is None:
        return None
    rc = rag[1]
    data = [i for i in range(rag[0] + 1, len(rows)) if TABLE_REF.match(_clean(_cell(rows[i], rc)) or "")]
    if not data:
        return None
    first = data[0]
    block = rows[:first]
    found = [_find_label(block, labels, before=rc) for labels in (LINE_LABELS, UNIT_LABELS, DP_LABELS)]
    top = min([rag[0]] + [f[0] for f in found if f])
    line, unit, dp = found

    def is_column(pos, numeric: bool) -> bool:  # labels a column of per-line values, not a header row
        if pos is None or (line is not None and pos[1] == line[1]):
            return False
        vals = [_cell(rows[i], pos[1]) for i in data]
        hits = sum(1 for v in vals if (tabular.read_number(v).value is not None) == numeric and _clean(v))
        return hits * 2 >= len(vals)

    unit_col = unit[1] if is_column(unit, numeric=False) else None
    dp_col = dp[1] if is_column(dp, numeric=True) else None

    def own_row(pos, col) -> int | None:  # a Units or DPs row; some tables have both the row and a column
        if pos is None:
            return None
        if col is None or (line is not None and pos[0] != line[0]):
            return pos[0]
        return None

    unit_row, dp_row = own_row(unit, unit_col), own_row(dp, dp_col)
    if line is not None:
        line_col = line[1]
    else:  # the leftmost column that holds text on most table lines
        counts = Counter(
            next((j for j, v in enumerate(rows[i][:rc]) if j > 0 and isinstance(v, str) and v.strip()), None)
            for i in data
        )
        line_col = min((j for j, n in counts.items() if j is not None and n * 2 >= len(data)), default=1)
    first_value = max(c for c in (line_col, unit_col, dp_col) if c is not None) + 1
    labels = LINE_LABELS | UNIT_LABELS | DP_LABELS | RAG_LABELS
    for i in range(top, first):  # label cells are not headings
        rows[i] = [None if norm_label(v) in labels else v for v in rows[i]]
    header_rows = [i for i in range(top, first) if i not in (unit_row, dp_row)]
    sections = []
    for group_headers, group_rows, units, dps in _sections(rows, header_rows, data, first_value, rc, unit_row, dp_row):
        cols = list(range(first_value, rc))
        paths = _paths(rows, group_headers, cols)
        cols = [c for c in cols if paths[c] or any(_clean(_cell(rows[i], c)) for i in group_rows)]
        attrs = {c: _attribute(paths[c]) for c in cols}
        unit_by_col = _filled(rows, units, cols) if units is not None else None
        if unit_by_col:  # the same heading over different units (for example % and £m)
            same = Counter((a or "").lower() for a in attrs.values())
            for c in cols:
                if same[(attrs[c] or "").lower()] > 1 and unit_by_col[c]:
                    attrs[c] = f"{attrs[c]} ({unit_by_col[c]})" if attrs[c] else unit_by_col[c]
        sections.append(
            {
                "data_rows": group_rows,
                "value_cols": cols,
                "attributes": attrs,
                "units": unit_by_col,
                "dps": _filled(rows, dps, cols) if dps is not None else None,
                "bon": _bon_offset(rows, group_headers, rc, {c: paths[c] for c in cols}),
            }
        )
    return {
        "rag_col": rc,
        "line_col": line_col,
        "first_value_col": first_value,
        "unit_col": unit_col,
        "dp_col": dp_col,
        "header_rows": header_rows,
        "title_rows": list(range(0, top)),
        "sections": sections,
    }


def _sections(rows, header_rows, data, first_value, rc, unit_row, dp_row):
    """(header rows, table lines, units row, DPs row) groups. A row inside the table that is not a
    table line but has text headings over the value columns relabels the columns for the lines
    below it; a Units or DPs row beside such headings applies to that group only."""
    data_set = set(data)
    groups, current, pending = [], [], []
    headers, units, dps = header_rows, unit_row, dp_row
    pending_units = pending_dps = None
    for i in range(data[0], data[-1] + 1):
        if i in data_set:
            if pending:
                if current:
                    groups.append((headers, current, units, dps))
                headers, current, pending = pending, [], []
                units = pending_units if pending_units is not None else unit_row
                dps = pending_dps if pending_dps is not None else dp_row
                pending_units = pending_dps = None
            current.append(i)
            continue
        label = {norm_label(_cell(rows[i], c)) for c in range(first_value)}
        if pending and label & UNIT_LABELS:
            pending_units = i
            continue
        if pending and label & DP_LABELS:
            pending_dps = i
            continue
        cells = [_cell(rows[i], c) for c in range(first_value, rc)]
        if any(isinstance(v, str) and _clean(v) and tabular.read_number(v).value is None for v in cells):
            pending.append(i)
        elif pending and not any(_clean(v) for v in cells):
            continue  # a blank row between heading rows
        else:
            pending, pending_units, pending_dps = [], None, None
    if current:
        groups.append((headers, current, units, dps))
    return groups


def _filled(rows, i, cols) -> dict[int, str | None]:
    """A units or DPs row read across merged cells: a blank cell takes the value to its left."""
    out, last = {}, None
    for c in cols:
        v = _clean(_cell(rows[i], c))
        last = v if v is not None else last
        out[c] = last
    return out


def _bon_offset(rows, header_rows, rc, paths) -> int | None:
    """Column offset from each value cell to its item reference in the Bon grid, if the sheet has one."""
    start = next(
        (j for row in rows[:5] for j, v in enumerate(row) if j > rc and norm_label(v) == BON_TITLE),
        None,
    )
    if start is None:
        return None
    width = max((len(r) for r in rows[: HEADER_SCAN * 4]), default=0)
    bon_paths = _paths(rows, header_rows, list(range(start, width)))
    key = lambda p: tuple(x.lower() for x in p)  # noqa: E731
    named = [c for c, p in paths.items() if p]
    best, best_hits = None, 0
    for d in range(start - min(paths, default=start), width):
        hits = sum(1 for c in named if key(bon_paths.get(c + d, [])) == key(paths[c]))
        if hits > best_hits:
            best, best_hits = d, hits
    return best if named and best_hits * 5 >= len(named) * 3 else None


def _company_and_year(sheets, aliases) -> tuple[Counter, Counter]:
    """Companies named in each table's title lines (the first rows) and the year ends its titles state.
    Lower title rows can be section headings that name other companies, so they are not read for this."""
    companies, years = Counter(), Counter()
    for _, rows, t in sheets:
        for i in t["title_rows"]:
            for v in rows[i][: t["rag_col"]]:
                text = _clean(v)
                if not text:
                    continue
                if i < TITLE_COMPANY_ROWS and text.lower() in aliases:
                    companies[aliases[text.lower()]] += 1
                for y in YEAR_END.findall(text):
                    years[int(y)] += 1
    return companies, years


def _read_line(result, asset, registered, company, expected, sheet, rows, t, sec, i) -> int:
    """Append one table line's values (or rejects) to `result`. Returns the number of blank cells."""
    row = rows[i]
    blanks = 0
    rag = _clean(row[t["rag_col"]])
    line = next(
        (
            _clean(_cell(row, c))
            for c in range(t["line_col"], t["first_value_col"])
            if isinstance(_cell(row, c), str) and _clean(_cell(row, c))
        ),
        None,
    )
    line_unit = _clean(_cell(row, t["unit_col"])) if t["unit_col"] is not None else None
    if line_unit and SEE_HEADING.search(line_unit):
        line_unit = None
    line_dps = tabular.read_number(_cell(row, t["dp_col"])) if t["dp_col"] is not None else None
    for c in sec["value_cols"]:
        raw = _cell(row, c)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            blanks += 1
            continue
        locator = f"file={asset['file_name']};sheet={sheet};cell={_col_letter(c)}{i + 1}"
        num = tabular.read_number(raw)
        if num.value is None:
            result.rejects.append(
                _reject(
                    asset,
                    locator,
                    f"VALUE_{num.reason.upper()}",
                    f"value is {num.reason}; not loaded and not treated as zero",
                    num.raw,
                )
            )
            continue
        unit = line_unit or (sec["units"][c] if sec["units"] else None)
        dps = line_dps if line_dps is not None and line_dps.value is not None else None
        if dps is None and sec["dps"]:
            dps = tabular.read_number(sec["dps"][c])
        ref = _clean(_cell(row, c + sec["bon"])) if sec["bon"] is not None else None
        attribute_raw = sec["attributes"][c]
        result.rows.append(
            {
                **_provenance(asset, locator, CONTRACT),
                "company_code_raw": company.company_name,
                "company_code": company.company_code,
                "reporting_area": "whole_company",
                "financial_year_raw": registered.financial_year,
                "report_year": expected.report_year,
                "sheet_name": sheet,
                "item_reference": ref if ref and BON_CODE.match(ref) else None,
                "line_description": line,
                "units": unit,
                "decimal_places": int(dps.value) if dps is not None and dps.value is not None else None,
                "rag_reference": rag,
                "attribute_raw": attribute_raw,
                "attribute": normalise_attribute(attribute_raw),
                "attribute_financial_year": attribute_financial_year(attribute_raw),
                "value": num.value,
                "value_raw": num.raw,
                "rag_version": None,
            }
        )
    return blanks


def parse_company_apr(path: str, asset: dict, registered: Asset, companies: dict[str, Company]) -> ParseResult:
    """One company's APR workbook for one financial year (both recorded on the registry asset)."""
    if not asset["file_name"].lower().endswith((".xlsx", ".xlsm")):
        raise ContractError(f"{asset['file_name']}: expected an Excel workbook")
    expected = periods.parse_financial_year(registered.financial_year)
    company = companies[registered.company_code]
    result = ParseResult()
    sheets, skipped = [], {"not_a_table": [], "restricted": []}
    wb = open_workbook(path)
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            t = _table(rows)
            if t is None:
                skipped["not_a_table"].append(ws.title)
                continue
            block = [_clean(v) or "" for i in t["header_rows"] for v in rows[i]]
            if any(RESTRICTED.search(b) for b in block):
                skipped["restricted"].append(ws.title)
                continue
            sheets.append((ws.title, rows, t))
    finally:
        wb.close()
    if not sheets:
        raise ContractError(f"{asset['file_name']}: no sheet matches the APR table layout", skipped)

    named, years = _company_and_year(sheets, company_alias_index(companies))
    others = sorted(c for c in named if c != company.company_code)
    if others or not named:
        raise ContractError(
            f"{asset['file_name']}: tables name {sorted(named) or 'no company'}, "
            f"but the asset is registered for {company.company_code}",
            {"companies_named": dict(named)},
        )
    if years and years.most_common(1)[0][0] != expected.report_year:
        raise ContractError(
            f"{asset['file_name']}: tables report years ending {dict(years)}, "
            f"but the asset is registered for {registered.financial_year}",
            {"year_ends": dict(years)},
        )

    blanks = 0
    for sheet, rows, t in sheets:
        for sec in t["sections"]:
            for i in sec["data_rows"]:
                blanks += _read_line(result, asset, registered, company, expected, sheet, rows, t, sec, i)
    for r in result.rows:
        r["_key"] = f"{r['rag_reference']}|{(r['attribute'] or '').lower()}"
    _quarantine_conflicts(result, ("_key", "report_year"), asset)
    for r in result.rows:
        r.pop("_key", None)
    result.evidence = {
        "contract": CONTRACT,
        "tables": len(sheets),
        "tables_with_item_references": sum(
            1 for _, _, t in sheets if any(sec["bon"] is not None for sec in t["sections"])
        ),
        "skipped_sheets": skipped,
        "blank_cells": blanks,
        "rows": len(result.rows),
        "rejects": len(result.rejects),
    }
    return result
