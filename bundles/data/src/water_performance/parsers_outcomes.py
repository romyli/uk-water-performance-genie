"""Parsers for Ofwat outcome workbooks: PR19 (2020-25) performance and ODIs, PR24 (2025-30) levels.

Both workbooks put a block label (for example "2020-25 actual performance") above a row of year
columns. The contract finds the header row by its identifying columns, reads the block label row
above it, and assigns each year column to the block whose label starts at or before it. Nothing
depends on fixed column letters.
"""

from __future__ import annotations

import re

from . import periods, tabular
from .parsers import ParseResult, _provenance, _quarantine_conflicts, _reject
from .registry import Company, company_alias_index
from .tabular import Column, ContractError

to_minutes = tabular.to_minutes
is_time_unit = tabular.is_time_unit
read_value = tabular.read_value


def _blocks(label_row, header_row, wanted: dict[str, tuple[str, ...]]) -> dict[tuple[str, int], int]:
    """Map (block, report_year) -> column, from a block label row above a header row of years."""
    starts = []
    for j, v in enumerate(label_row or ()):
        text = tabular.norm_label(v)
        for block, needles in wanted.items():
            if text and any(tabular.norm_label(n) in text for n in needles):
                starts.append((j, block))
                break
    starts.sort()
    out: dict[tuple[str, int], int] = {}
    for j, v in enumerate(header_row):
        try:
            year = periods.parse_financial_year(v).report_year
        except periods.PeriodError:
            continue
        owner = [b for s, b in starts if s <= j]
        if owner:
            out.setdefault((owner[-1], year), j)
    return out


def _scan(rows, columns, scan_rows=40):
    buffered = []
    it = iter(rows)
    for _ in range(scan_rows):
        try:
            buffered.append(next(it))
        except StopIteration:
            break
    header = tabular.find_header(buffered, columns, scan_rows)
    label_row = buffered[header.row_index - 1] if header.row_index > 0 else ()
    return header, label_row, buffered, it


# --- PR19 -------------------------------------------------------------------------------------

PR19_COLUMNS = (
    Column("company", ("Company",)),
    Column("pc_id", ("Unique ID",)),
    Column("pc_name", ("PC name",)),
    Column("pc_short_description", ("PC short description",), required=False),
    Column("apr_table", ("APR table",), required=False),
    Column("pc_type", ("PC type",)),
    Column("outcome", ("Outcome",), required=False),
    Column("odi_type", ("ODI type",), required=False),
    Column("odi_form", ("ODI form",), required=False),
    Column("odi_timing", ("ODI timing",), required=False),
    Column("unit", ("PC unit",)),
    Column("unit_description", ("PC unit description",), required=False),
    Column("decimal_places", ("Decimal places",), required=False),
    Column("direction", ("Direction of improving performance",), required=False),
    Column("primary_category", ("Primary category",), required=False),
    Column("comparable_pc", ("Common and comparable bespoke PC",), required=False),
)
PR19_BLOCKS = {
    "pcl": ("2020-25 Performance commitment levels",),
    "actual": ("2020-25 actual performance",),
    "odi_payment": ("2020-25 actual outcome delivery incentive payments",),
    "pcl_met": ("PCL met",),
    "standard_collar": ("Standard underperformance payment collar",),
    "standard_cap": ("Standard outperformance payment cap",),
    "underperformance_deadband": ("Underperformance payment deadband",),
    "outperformance_deadband": ("Outperformance payment deadband",),
    "enhanced_collar": ("Enhanced underperformance payment collar",),
    "enhanced_cap": ("Enhanced outperformance payment cap",),
    "odi_may_apply": ("Financial ODI may accrue or apply",),
    "incentive_rates": ("Incentives rates",),
}
PR19_PRICE_BASE = "2017-18 prices (CPIH deflated)"


def _workbook_note(path: str, pattern: str) -> str | None:
    for _, rows in tabular.xlsx_sheets(path):
        for i, row in enumerate(rows):
            if i > 12:
                break
            for v in row:
                if v and re.search(pattern, str(v), re.IGNORECASE):
                    return str(v).strip()
    return None


def parse_pr19(path: str, asset: dict, companies: dict[str, Company]) -> ParseResult:
    """One row per company, PR19 performance commitment and report year: PCL, actual, PCL met and
    ODI payment, plus the price-control net ODI payments (post aggregate sharing)."""
    contract = "ofwat_pr19_odi_v1"
    aliases = company_alias_index(companies)
    result = ParseResult()
    found_pc = found_pc_sheet = False
    pc_rows: list[dict] = []
    control_rows: list[dict] = []
    for sheet, sheet_rows in tabular.xlsx_sheets(path):
        rows = list(sheet_rows)  # sheets are small; each contract below reads the rows independently
        try:
            header, label_row, buffered, rest = _scan(rows, PR19_COLUMNS)
        except ContractError:
            header = None
        if header is not None:
            found_pc_sheet = True
            blocks = _blocks(label_row, buffered[header.row_index], PR19_BLOCKS)
            missing = [b for b in ("pcl", "actual", "odi_payment", "pcl_met") if not any(k[0] == b for k in blocks)]
            if missing:
                raise ContractError(
                    f"{sheet}: year blocks {missing} not found above the header row",
                    {"block_labels": [str(v)[:60] for v in label_row if v]},
                )
            found_pc = True
            pc_rows += list(_pr19_pc_rows(sheet, header, blocks, buffered, rest, asset, aliases, result, contract))
            continue
        control_rows += _pr19_price_control(sheet, rows, asset, aliases, result, contract)
    if not found_pc:
        raise ContractError(
            f"{asset['file_name']}: no sheet matches the PR19 performance commitment layout",
            {"found_header_sheet": found_pc_sheet},
        )
    note = _workbook_note(path, r"pre aggregate sharing")
    for r in pc_rows:
        r["sharing_basis"] = "pre aggregate sharing" if note else None
    result.rows = pc_rows
    _quarantine_conflicts(result, ("company_code", "pc_id", "report_year", "measure"), asset)
    controls = ParseResult(rows=control_rows)
    _quarantine_conflicts(controls, ("company_code", "item_reference", "report_year"), asset)
    result.rejects += controls.rejects
    result.evidence = {
        "contract": contract,
        "pc_rows": len(result.rows),
        "price_control_rows": len(controls.rows),
        "price_control": controls.rows,
    }
    return result


def _pr19_pc_rows(sheet, header, blocks, buffered, rest, asset, aliases, result, contract):
    cols = header.columns

    def all_rows():
        yield from buffered[header.row_index + 1 :]
        yield from rest

    for offset, row in enumerate(all_rows(), start=header.row_index + 2):
        cells = list(row)
        get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else None  # noqa: E731
        pc_id = tabular.text(get("pc_id"))
        if not pc_id:
            continue
        locator = f"file={asset['file_name']};sheet={sheet};row={offset}"
        code = aliases.get((tabular.text(get("company")) or "").lower())
        if code is None:
            result.rejects.append(
                _reject(asset, locator, "UNKNOWN_COMPANY", "company is not in the crosswalk", get("company"))
            )
            continue
        unit = tabular.text(get("unit"))
        base = {
            **_provenance(asset, locator, contract),
            "company_code": code,
            "pc_id": pc_id,
            "pc_name": tabular.text(get("pc_name")),
            "pc_short_description": tabular.text(get("pc_short_description")),
            "apr_table": tabular.text(get("apr_table")),
            "pc_type": tabular.text(get("pc_type")),
            "outcome": tabular.text(get("outcome")),
            "odi_type": tabular.text(get("odi_type")),
            "odi_form": tabular.text(get("odi_form")),
            "odi_timing": tabular.text(get("odi_timing")),
            "unit_raw": unit,
            "unit_description": tabular.text(get("unit_description")),
            "direction": tabular.text(get("direction")),
            "primary_category": tabular.text(get("primary_category")),
            "comparable_pc": tabular.text(get("comparable_pc")),
        }
        years = sorted({y for (_, y) in blocks})
        for year in years:
            for measure in (
                "pcl",
                "actual",
                "odi_payment",
                "standard_collar",
                "standard_cap",
                "underperformance_deadband",
                "outperformance_deadband",
            ):
                j = blocks.get((measure, year))
                if j is None or j >= len(cells):
                    continue
                raw = cells[j]
                if measure == "odi_payment":
                    n = tabular.read_number(raw)
                    value, reason, unit_loaded = n.value, n.reason, "£m"
                else:
                    value, reason, unit_loaded = read_value(raw, unit)
                if value is None:
                    if reason not in ("missing",):  # empty cells are normal: not every PC has every measure
                        result.rejects.append(
                            _reject(
                                asset,
                                f"{locator};col={j + 1}",
                                f"VALUE_{reason.upper()}",
                                f"{measure} {year}: value is {reason}",
                                raw,
                            )
                        )
                    continue
                met = cells[blocks[("pcl_met", year)]] if ("pcl_met", year) in blocks else None
                yield {
                    **base,
                    "record_locator": f"{locator};col={j + 1}",
                    "measure": measure,
                    "report_year": year,
                    "value": value,
                    "value_raw": None if raw is None else str(raw),
                    "unit": unit_loaded,
                    "price_base": PR19_PRICE_BASE if measure == "odi_payment" else None,
                    "pcl_met": tabular.text(met),
                }


def _pr19_price_control(sheet, rows, asset, aliases, result, contract) -> list[dict]:
    """Rows of company, item reference, description and five year columns in £m."""
    out = []
    buffered = list(rows)
    year_row = next(
        (
            i
            for i, r in enumerate(buffered[:20])
            if sum(1 for v in r if v and re.fullmatch(r"20\d\d-\d\d", str(v).strip())) >= 5
        ),
        None,
    )
    if year_row is None:
        return out
    title = " ".join(str(v) for r in buffered[:year_row] for v in r if v)
    if "net outcome delivery incentive" not in title.lower():
        return out
    price = re.search(r"in (\d{4}-\d{2}) prices", title)
    sharing = "post aggregate sharing" if re.search(r"post aggregate sharing", title, re.IGNORECASE) else None
    years = {}
    for j, v in enumerate(buffered[year_row]):
        try:  # single financial years only; a 2020-25 total column is not a year
            years[j] = periods.parse_financial_year(v).report_year
        except periods.PeriodError:
            continue
    for offset, row in enumerate(buffered[year_row + 2 :], start=year_row + 3):
        cells = list(row)
        texts = [(j, tabular.text(v)) for j, v in enumerate(cells[: min(years)]) if tabular.text(v)]
        if len(texts) < 3:
            continue
        code = aliases.get(texts[0][1].lower())
        if code is None:
            continue
        ref, desc = texts[1][1], texts[2][1]
        for j, year in years.items():
            locator = f"file={asset['file_name']};sheet={sheet};row={offset};col={j + 1}"
            n = tabular.read_number(cells[j] if j < len(cells) else None)
            if n.value is None:
                if n.reason != "missing":
                    result.rejects.append(_reject(asset, locator, f"VALUE_{n.reason.upper()}", f"{ref} {year}", n.raw))
                continue
            out.append(
                {
                    **_provenance(asset, locator, contract),
                    "company_code": code,
                    "item_reference": ref,
                    "description": desc,
                    "report_year": year,
                    "value": n.value,
                    "value_raw": n.raw,
                    "unit": "£m",
                    "price_base": f"{price.group(1)} prices" if price else None,
                    "sharing_basis": sharing,
                }
            )
    return out


# --- PR24 -------------------------------------------------------------------------------------

PR24_COLUMNS = (
    Column("company", ("Company",)),
    Column("measure", ("Measure",)),
    Column("setting", ("Setting at a common or a company specific level",), required=False),
    Column("unit", ("PCL Units",)),
)
PR24_BLOCKS = {
    "pcl_uptime": ("Performance Commitment Levels - Uptime",),
    "pcl": ("Performance Commitment Levels",),
    "deadband": ("Deadband",),
    "enhanced_threshold": ("Enhanced outperformace threshold", "Enhanced outperformance threshold"),
}


def parse_pr24(path: str, asset: dict, companies: dict[str, Company]) -> ParseResult:
    """One row per company, PR24 performance commitment, measure and report year (2025-26 to 2029-30)."""
    contract = "ofwat_pr24_outcomes_v1"
    aliases = company_alias_index(companies)
    result = ParseResult()
    matched = False
    for sheet, rows in tabular.xlsx_sheets(path):
        try:
            header, label_row, buffered, rest = _scan(rows, PR24_COLUMNS, scan_rows=12)
        except ContractError:
            continue
        hdr = buffered[header.row_index]
        blocks = _blocks(label_row, hdr, PR24_BLOCKS)
        if not any(k[0] == "pcl" for k in blocks):
            continue
        # The PCL block's first column is a 'Baseline' (2024-25) column without a year label.
        pcl_first = min(j for (b, _), j in blocks.items() if b == "pcl")
        baseline_col = pcl_first - 1 if tabular.norm_label(hdr[pcl_first - 1]) == "baseline" else None
        matched = True
        cols = header.columns

        def all_rows(buffered=buffered, rest=rest, header=header):
            yield from buffered[header.row_index + 1 :]
            yield from rest

        for offset, row in enumerate(all_rows(), start=header.row_index + 2):
            cells = list(row)
            get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else None  # noqa: E731
            measure_name = (tabular.text(get("measure")) or "").replace("\xa0", " ").replace(" ", " ").strip()
            company_raw = tabular.text(get("company"))
            if not measure_name or not company_raw:
                continue
            locator = f"file={asset['file_name']};sheet={sheet};row={offset}"
            code = aliases.get(company_raw.lower())
            if code is None:
                result.rejects.append(
                    _reject(asset, locator, "UNKNOWN_COMPANY", "company is not in the crosswalk", company_raw)
                )
                continue
            unit = tabular.text(get("unit"))
            base = {
                **_provenance(asset, locator, contract),
                "company_code": code,
                "pc_name": measure_name,
                "setting_level": tabular.text(get("setting")),
                "unit_raw": unit,
            }
            targets = [(m, y, j) for (m, y), j in blocks.items()]
            if baseline_col is not None:
                targets.append(("pcl_baseline", 2025, baseline_col))
            for measure, year, j in sorted(targets, key=lambda t: (t[0], t[1])):
                raw = cells[j] if j < len(cells) else None
                value, reason, unit_loaded = read_value(raw, unit)
                if value is None:
                    if reason not in ("missing",) and str(raw).strip().upper() not in ("N/A", "NA"):
                        result.rejects.append(
                            _reject(
                                asset,
                                f"{locator};col={j + 1}",
                                f"VALUE_{reason.upper()}",
                                f"{measure} {year}: value is {reason}",
                                raw,
                            )
                        )
                    continue
                result.rows.append(
                    {
                        **base,
                        "record_locator": f"{locator};col={j + 1}",
                        "measure": measure,
                        "report_year": year,
                        "value": value,
                        "value_raw": str(raw),
                        "unit": unit_loaded,
                    }
                )
    if not matched:
        raise ContractError(f"{asset['file_name']}: no sheet matches the PR24 'View by PC' layout")
    _quarantine_conflicts(result, ("company_code", "pc_name", "measure", "report_year"), asset)
    result.evidence = {"contract": contract, "rows": len(result.rows)}
    return result
