"""Source parsers. One contract per source family; each returns Silver rows and rejects.

A parser receives one archived asset version and its manifest row. It finds its tables by
content, reads every value with `tabular.read_number`, and reports anything it cannot read as a
reject with a locator. It never coerces a missing or invalid value to zero and never guesses a
company or period.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field

from . import periods, tabular
from .registry import Company, StreamItem, company_alias_index, load_reporting_areas
from .tabular import Column, ContractError

PARSER_VERSION = "1.1.0"


@dataclass
class ParseResult:
    rows: list[dict] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def _reject(asset: dict, locator: str, code: str, reason: str, original=None, evidence=None) -> dict:
    return {
        "source_id": asset["source_id"],
        "asset_key": asset["asset_key"],
        "asset_version": asset["asset_version"],
        "record_locator": locator,
        "reason_code": code,
        "reason": reason,
        "original_value": None if original is None else str(original)[:2000],
        "evidence": evidence,
    }


def _provenance(asset: dict, locator: str, contract: str) -> dict:
    return {
        "source_id": asset["source_id"],
        "asset_key": asset["asset_key"],
        "asset_version": asset["asset_version"],
        "sha256": asset["sha256"],
        "release_label": asset.get("release_label"),
        "record_locator": locator,
        "parser_version": f"{contract}@{PARSER_VERSION}",
    }


def _quarantine_conflicts(result: ParseResult, key_fields: tuple[str, ...], asset: dict) -> None:
    """Rows that share a key but disagree on value are removed and rejected; exact repeats collapse."""
    groups: dict[tuple, list[dict]] = {}
    for r in result.rows:
        groups.setdefault(tuple(r[k] for k in key_fields), []).append(r)
    kept = []
    for key, rows in groups.items():
        values = {r["value"] for r in rows}
        if len(values) == 1:
            kept.append(rows[0])
            continue
        for r in rows:
            result.rejects.append(
                _reject(
                    asset,
                    r["record_locator"],
                    "CONFLICTING_VALUES",
                    f"{len(rows)} rows share the key {dict(zip(key_fields, key))} "
                    f"with different values {sorted(values)}",
                    r["value_raw"],
                )
            )
    result.rows = kept


# --- Ofwat WCPR normalised open data -----------------------------------------------------------

WCPR_COLUMNS = (
    Column("company_name", ("COMPANY_NAME", "Company name", "Company")),
    Column("company_code", ("COMPANY_ABBREVIATION", "Company abbreviation", "Company code", "Acronym")),
    Column("item_reference", ("UNIQUE_ITEM_REFERENCE", "Unique item reference", "Item reference", "Reference")),
    Column("short_description", ("SHORT_DESCRIPTION", "Short description", "Description")),
    Column("rag_reference", ("RAG", "RAG reference", "RAG 4 reference"), required=False),
    Column("year", ("YEAR", "Year", "Financial year", "Reporting year")),
    Column("value", ("VALUE", "Value")),
    Column("units", ("UNITS", "Units", "Unit")),
)


def parse_wcpr(path: str, asset: dict, companies: dict[str, Company]) -> ParseResult:
    contract = "ofwat_wcpr_normalised_v1"
    aliases = company_alias_index(companies)
    areas = load_reporting_areas()
    result = ParseResult()
    name = asset["file_name"].lower()
    if name.endswith(".csv"):
        tables = [("(csv)", tabular.csv_rows(path))]
    elif name.endswith((".xlsx", ".xlsm")):
        tables = tabular.xlsx_sheets(path)  # lazy: the workbook stays open while its rows are read
    else:
        raise ContractError(f"{asset['file_name']}: expected a CSV or Excel workbook")
    matched = []
    for sheet, rows in tables:
        rows = iter(rows)
        buffered = []
        for _ in range(40):
            try:
                buffered.append(next(rows))
            except StopIteration:
                break
        try:
            header = tabular.find_header(buffered, WCPR_COLUMNS)
        except ContractError as e:
            matched.append((sheet, None, e.evidence))
            continue
        matched.append((sheet, header, header.labels))
        cols = header.columns

        def all_rows():
            yield from buffered[header.row_index + 1 :]
            yield from rows

        for offset, row in enumerate(all_rows(), start=header.row_index + 2):
            locator = f"file={asset['file_name']};sheet={sheet};row={offset}"
            cells = list(row) + [None] * (max(cols.values()) + 1 - len(row))
            if all(c in (None, "") for c in cells):
                continue
            get = lambda k: cells[cols[k]] if k in cols else None  # noqa: E731
            raw_code = tabular.text(get("company_code"))
            area = "whole_company"
            if raw_code in areas:  # a regional row, reported under its parent company
                code, area = areas[raw_code]
            else:
                code = aliases.get((raw_code or "").lower()) or aliases.get(
                    (tabular.text(get("company_name")) or "").lower()
                )
            if code is None:
                result.rejects.append(
                    _reject(asset, locator, "UNKNOWN_COMPANY", "company is not in the company crosswalk", raw_code)
                )
                continue
            try:
                fy = periods.parse_financial_year(get("year"))
            except periods.PeriodError as e:
                result.rejects.append(_reject(asset, locator, "INVALID_PERIOD", str(e), get("year")))
                continue
            units = tabular.text(get("units"))
            value, reason, units_loaded = tabular.read_value(get("value"), units)
            if value is None:
                result.rejects.append(
                    _reject(
                        asset,
                        locator,
                        f"VALUE_{reason.upper()}",
                        f"value is {reason}; not loaded and not treated as zero",
                        get("value"),
                    )
                )
                continue
            item = tabular.text(get("item_reference"))
            if not item:
                result.rejects.append(_reject(asset, locator, "MISSING_ITEM_REFERENCE", "row has no item reference"))
                continue
            result.rows.append(
                {
                    **_provenance(asset, locator, contract),
                    "company_name_raw": tabular.text(get("company_name")),
                    "company_code_raw": raw_code,
                    "company_code": code,
                    "reporting_area": area,
                    "item_reference": item,
                    "short_description": tabular.text(get("short_description")),
                    "rag_reference": tabular.text(get("rag_reference")),
                    "financial_year_raw": tabular.text(get("year")),
                    "report_year": fy.report_year,
                    "value": value,
                    "value_raw": None if get("value") is None else str(get("value")),
                    "units": units_loaded,
                }
            )
    if not any(h is not None for _, h, _ in matched):
        raise ContractError(
            f"{asset['file_name']}: no sheet matches the WCPR normalised layout",
            {"sheets": [{"sheet": s, "evidence": ev} for s, _, ev in matched]},
        )
    result.evidence = {"contract": contract, "sheets": [{"sheet": s, "header": ev} for s, h, ev in matched if h]}
    _quarantine_conflicts(result, ("company_code", "reporting_area", "item_reference", "report_year"), asset)
    return result


# --- Stream APR --------------------------------------------------------------------------------

STREAM_COLUMNS = (
    Column("company", ("Company",)),
    Column("year", ("Year",)),
    Column("item_reference", ("Item_reference", "Item reference"), required=False),
    Column("line_description", ("Line_Description", "Line description")),
    Column("units", ("Units", "Unit")),
    Column("dps", ("DPs", "Decimal places"), required=False),
    Column("rag_reference", ("RAG_4_reference", "RAG 4 reference", "RAG reference")),
    Column("attribute", ("Attribute",)),
    Column("value", ("Value",)),
    Column("object_id", ("ObjectId", "OBJECTID", "FID"), required=False),
)


def normalise_attribute(raw: str | None) -> str | None:
    """Join attribute parts with ' | ' whatever separator the publisher used ('-' or '|')."""
    if raw is None:
        return None
    parts = re.split(r"\s+\|\s+|\s*\|\s*|\s+-\s+|(?<=[A-Za-z)])-\s+", raw)
    return " | ".join(p.strip() for p in parts if p.strip())


_ATTR_FY = re.compile(r"\b(20\d\d[-/]\d\d)\b")


def attribute_financial_year(attribute: str | None) -> int | None:
    """The financial year an attribute names, e.g. 'Performance level | actual (2023-24)' -> 2024."""
    m = _ATTR_FY.search(attribute or "")
    if not m:
        return None
    try:
        return periods.parse_financial_year(m.group(1)).report_year
    except periods.PeriodError:
        return None


def _stream_records(path: str, asset: dict, item_type: str):
    """(locator, logical record) pairs from an API snapshot or a CSV item."""
    name = asset["file_name"].lower()
    if name.endswith(".jsonl.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            first = None
            for line_no, line in enumerate(f, start=1):
                rec = json.loads(line)
                if first is None:
                    first = rec
                    header = tabular.find_header([list(rec.keys())], STREAM_COLUMNS, scan_rows=1)
                    keys = list(rec.keys())
                    names = {logical: keys[i] for logical, i in header.columns.items()}
                yield (
                    f"api_item={asset['api_item_id']};{names.get('object_id', 'line')}={rec.get(names.get('object_id'), line_no)}",
                    {logical: rec.get(k) for logical, k in names.items()},
                    rec.get(names.get("object_id"), line_no),
                )
    elif name.endswith(".csv") or item_type == "csv":
        rows = tabular.csv_rows(path)
        buffered = [next(rows)]
        header = tabular.find_header(buffered, STREAM_COLUMNS, scan_rows=1)
        for line_no, row in enumerate(rows, start=2):
            cells = list(row) + [None] * (max(header.columns.values()) + 1 - len(row))
            rec = {logical: cells[i] for logical, i in header.columns.items()}
            yield f"file={asset['file_name']};row={line_no}", rec, line_no
    else:
        raise ContractError(f"{asset['file_name']}: expected an API snapshot or CSV item")


def parse_stream(path: str, asset: dict, item: StreamItem, rag_version: str | None) -> ParseResult:
    contract = "stream_apr_v1"
    result = ParseResult()
    expected_fy = periods.parse_financial_year(item.financial_year)
    for locator, rec, object_id in _stream_records(path, asset, item.item_type):
        code_raw = tabular.text(rec.get("company"))
        if code_raw != item.source_company_code:
            result.rejects.append(
                _reject(
                    asset,
                    locator,
                    "COMPANY_CONFLICT",
                    f"record company {code_raw!r} differs from the registered item company "
                    f"{item.source_company_code!r}",
                    code_raw,
                )
            )
            continue
        try:
            fy = periods.parse_financial_year(rec.get("year"))
        except periods.PeriodError as e:
            result.rejects.append(_reject(asset, locator, "INVALID_PERIOD", str(e), rec.get("year")))
            continue
        if fy != expected_fy:
            result.rejects.append(
                _reject(
                    asset,
                    locator,
                    "PERIOD_CONFLICT",
                    f"record year {rec.get('year')!r} differs from the item year {item.financial_year}",
                    rec.get("year"),
                )
            )
            continue
        num = tabular.read_number(rec.get("value"))
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
        dps = tabular.read_number(rec.get("dps"))
        result.rows.append(
            {
                **_provenance(asset, locator, contract),
                "item_id": item.item_id,
                "object_id": int(object_id),
                "company_code_raw": code_raw,
                "company_code": item.company_code,
                "reporting_area": item.reporting_area,
                "financial_year_raw": tabular.text(rec.get("year")),
                "report_year": fy.report_year,
                "item_reference": tabular.text(rec.get("item_reference")),
                "line_description": tabular.text(rec.get("line_description")),
                "units": tabular.text(rec.get("units")),
                "decimal_places": int(dps.value) if dps.value is not None else None,
                "rag_reference": tabular.text(rec.get("rag_reference")),
                "attribute_raw": tabular.text(rec.get("attribute")),
                "attribute": normalise_attribute(tabular.text(rec.get("attribute"))),
                "attribute_financial_year": attribute_financial_year(tabular.text(rec.get("attribute"))),
                "value": num.value,
                "value_raw": num.raw,
                "rag_version": rag_version,
            }
        )
    for r in result.rows:  # item references are absent from some years, so fall back to the line identity
        r["_key"] = r["item_reference"] or f"{r['rag_reference']}|{r['line_description']}|{r['attribute']}"
    _quarantine_conflicts(result, ("_key", "report_year"), asset)
    for r in result.rows:
        r.pop("_key", None)
    result.evidence = {"contract": contract, "rows": len(result.rows), "rejects": len(result.rejects)}
    return result
