"""Company APR workbooks on Ofwat's pro forma template, using generated fixtures."""

from __future__ import annotations

import pytest

from fakes import write_company_apr_xlsx
from water_performance import parsers_apr, registry
from water_performance.registry import Asset
from water_performance.tabular import ContractError

COMPANIES = registry.load_companies()


def _parse(path, financial_year="2024-25", company_code="TMS"):
    asset = {
        "source_id": "COMPANY_APR",
        "asset_key": "tms_apr_tables_2024_25",
        "asset_version": 1,
        "sha256": "f" * 64,
        "release_label": "test",
        "file_name": path.name,
    }
    registered = Asset(
        "COMPANY_APR",
        asset["asset_key"],
        "test",
        "https://example.invalid/a.xlsx",
        "https://example.invalid",
        "hold",
        company_code=company_code,
        financial_year=financial_year,
    )
    return parsers_apr.parse_company_apr(str(path), asset, registered, COMPANIES)


def _by_key(result):
    return {(r["rag_reference"], r["attribute"]): r for r in result.rows}


def test_values_get_stream_style_attributes_units_and_item_references(artifact_dir):
    rows = _by_key(_parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx")))
    na = rows[("1A.1", "Adjustments | Non-appointed")]
    assert na["value"] == 157.162 and na["units"] == "£m" and na["decimal_places"] == 3
    assert na["item_reference"] == "BO1180NA" and na["line_description"] == "Revenue"
    assert na["record_locator"].endswith("sheet=1A;cell=H8")
    assert na["company_code"] == "TMS" and na["report_year"] == 2025 and na["reporting_area"] == "whole_company"
    assert rows[("1A.1", "Statutory")]["value"] == 2738.2
    assert rows[("1A.1", "Total appointed activities")]["item_reference"] == "BO1180"
    # A parent heading spans only the columns that have a heading under it.
    assert ("1A.1", "Total appointed activities | Total adjustments") not in rows


def test_units_and_decimal_places_given_as_header_rows(artifact_dir):
    rows = _by_key(_parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx")))
    customers = rows[("2F.1", "Number of customers")]
    assert customers["units"] == "000s" and customers["decimal_places"] == 3 and customers["value"] == 6000.5
    assert rows[("2F.1", "Revenue")]["units"] == "£m"


def test_headings_inside_a_table_relabel_the_lines_below(artifact_dir):
    rows = _by_key(_parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx")))
    assert rows[("4R.1", "Measured")]["value"] == 26.449
    assert rows[("4R.10", "Water | Measured")]["value"] == 3000.1
    assert rows[("4R.10", "Wastewater | Measured")]["value"] == 2500.3


def test_invalid_cells_are_rejected_with_their_address_and_blanks_are_not_values(artifact_dir):
    result = _parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx"))
    rejects = {r["record_locator"].split(";", 1)[1]: r["reason_code"] for r in result.rejects}
    assert rejects == {"sheet=1A;cell=G9": "VALUE_SPREADSHEET_ERROR"}
    rows = _by_key(result)
    assert ("1A.4", "Adjustments | Total adjustments") not in rows  # blank cell
    assert rows[("1A.4", "Adjustments | Non-appointed")]["value"] == -1123.461
    # "Copy" in the Bon grid is not an item reference.
    assert rows[("1A.4", "Statutory")]["item_reference"] == "BO2060STAT"
    assert all(r["rag_reference"] != "1A.4" or r["item_reference"] != "Copy" for r in result.rows)
    # Columns right of the RAG reference (comments, checks, the Bon grid) are never read as values.
    assert all(not r["record_locator"].endswith(("N8", "X8", "Z8")) for r in result.rows)


def test_debt_instrument_registers_are_never_read(artifact_dir):
    result = _parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx"))
    assert result.evidence["skipped_sheets"]["restricted"] == ["4B"]
    assert not [r for r in result.rows if r["rag_reference"].startswith("4B")]
    assert "Cover" in result.evidence["skipped_sheets"]["not_a_table"]


def test_same_heading_over_different_units_is_told_apart_by_unit(artifact_dir):
    rows = _by_key(_parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx", duplicate_heading=True)))
    assert rows[("2F.1", "Revenue (£m)")]["value"] == 1500.25
    assert rows[("2F.1", "Revenue (000s)")]["value"] == 6000.5


def test_same_heading_with_different_values_is_quarantined(artifact_dir):
    path = write_company_apr_xlsx(artifact_dir / "apr.xlsx", duplicate_heading=True, second_unit="£m")
    result = _parse(path)
    codes = [r["reason_code"] for r in result.rejects if "sheet=2F" in r["record_locator"]]
    assert codes == ["CONFLICTING_VALUES", "CONFLICTING_VALUES"]
    assert not [r for r in result.rows if r["rag_reference"] == "2F.1"]


@pytest.mark.parametrize(
    "kwargs,expect",
    [
        ({"company": "Severn Trent Water"}, "registered for TMS"),
        ({"year_end": 2024}, "registered for 2024-25"),
    ],
)
def test_workbook_for_another_company_or_year_fails_the_contract(artifact_dir, kwargs, expect):
    with pytest.raises(ContractError, match=expect):
        _parse(write_company_apr_xlsx(artifact_dir / "apr.xlsx", **kwargs))


def test_workbook_without_apr_tables_fails_the_contract(artifact_dir):
    import openpyxl

    from fakes import save_workbook

    wb = openpyxl.Workbook()
    wb.active["A1"] = "Line description"
    path = save_workbook(wb, artifact_dir / "other.xlsx")
    with pytest.raises(ContractError, match="no sheet matches the APR table layout"):
        _parse(path)


def test_registered_company_workbooks_are_held_until_reuse_terms_are_recorded():
    src = registry.load_registry().source("COMPANY_APR")
    assert src.status == "hold" and src.assets
    for a in src.assets:
        assert a.status == "hold" and a.hold_reason and a.company_code in COMPANIES
        assert a.financial_year and a.report_year_to == int("20" + a.financial_year[-2:])
