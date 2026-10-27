"""Content-based parsing, value safety and period/company rules, using generated fixtures."""

from __future__ import annotations

import gzip

import pytest

from fakes import save_workbook, stream_records, write_wcpr_csv, write_wcpr_xlsx
from water_performance import manifest, parsers, periods, registry, tabular

COMPANIES = registry.load_companies()


def _asset(file_name, **over):
    a = {
        "source_id": "OFWAT_WCPR",
        "asset_key": "a1",
        "asset_version": 1,
        "sha256": "f" * 64,
        "release_label": "test",
        "file_name": file_name,
        "api_item_id": None,
    }
    a.update(over)
    return a


# --- Values ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,value,reason",
    [
        (12, 12.0, None),
        (0, 0.0, None),
        ("1,234.5", 1234.5, None),
        ("(3.2)", -3.2, None),
        ("45%", 45.0, None),
        (None, None, "missing"),
        ("", None, "missing"),
        ("-", None, "placeholder"),
        ("n/a", None, "placeholder"),
        ("#N/A", None, "spreadsheet_error"),
        ("#DIV/0!", None, "spreadsheet_error"),
        ("#REF!", None, "spreadsheet_error"),
        ("=1+1", None, "not_numeric"),
        (True, None, "not_numeric"),
        ("abc", None, "not_numeric"),
        (float("nan"), None, "not_numeric"),
    ],
)
def test_values_are_never_coerced_to_zero(raw, value, reason):
    n = tabular.read_number(raw)
    assert (n.value, n.reason) == (value, reason)


def test_header_is_found_by_content_not_offset():
    cols = parsers.WCPR_COLUMNS
    rows = [["Title"], [], ["notes", "x"], [c.lower() for c in __import__("fakes").WCPR_HEADER]]
    h = tabular.find_header(rows, cols)
    assert h.row_index == 3 and h.columns["value"] == 6


def test_unsupported_layout_is_rejected_with_evidence():
    with pytest.raises(tabular.ContractError) as e:
        tabular.find_header([["Company", "Year"], ["Thames", "2024-25"]], parsers.WCPR_COLUMNS)
    assert e.value.evidence["first_rows"][0] == ["Company", "Year"]
    assert "UNIQUE_ITEM_REFERENCE" in e.value.evidence["expected"]["item_reference"]


# --- Periods -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,year",
    [
        ("2024-25", 2025),
        ("2023/24", 2024),
        ("2024-2025", 2025),
        ("2024 2025", 2025),
        ("FY2019-20", 2020),
        ("1999-00", 2000),
    ],
)
def test_financial_year_labels(label, year):
    assert periods.parse_financial_year(label).report_year == year


@pytest.mark.parametrize("label", ["2024", "2024-26", "24-25", "", None, "Q1 2024"])
def test_ambiguous_or_invalid_periods_are_rejected(label):
    with pytest.raises(periods.PeriodError):
        periods.parse_financial_year(label)


def test_price_controls_are_separate():
    assert periods.FinancialYear(2025).amp == "AMP7" and periods.FinancialYear(2025).price_review == "PR19"
    assert periods.FinancialYear(2026).amp == "AMP8" and periods.FinancialYear(2026).price_review == "PR24"
    assert periods.FinancialYear(2025).label == "2024-25"


# --- Companies ---------------------------------------------------------------------------------


def test_company_must_match_exactly():
    assert registry.resolve_company("TMS", COMPANIES).company_name == "Thames Water"
    with pytest.raises(registry.CompanyConfigError, match="Did you mean TMS"):
        registry.resolve_company("Thames", COMPANIES)
    with pytest.raises(registry.CompanyConfigError, match="Accepted codes: AFW, ANH"):
        registry.resolve_company("XYZ", COMPANIES)
    with pytest.raises(registry.CompanyConfigError):
        registry.resolve_company("tms", COMPANIES)


def test_crosswalk_service_types_are_valid():
    assert {c.service_type for c in COMPANIES.values()} <= set(registry.SERVICE_TYPES)
    assert COMPANIES["AFW"].service_type == "water_only" and COMPANIES["SVE"].service_type == "water_and_sewerage"


# --- WCPR --------------------------------------------------------------------------------------


def test_wcpr_csv_parses_and_rejects_with_reasons(artifact_dir):
    path = write_wcpr_csv(artifact_dir / "wcpr.csv", header_offset=2)
    r = parsers.parse_wcpr(str(path), _asset("wcpr.csv"), COMPANIES)
    keys = {(x["company_code"], x["item_reference"], x["report_year"]) for x in r.rows}
    assert keys == {("TMS", "APR3F_11", 2024), ("TMS", "APR3F_11", 2023), ("SVE", "APR3F_11", 2024)}
    codes = sorted(x["reason_code"] for x in r.rejects)
    assert codes == ["INVALID_PERIOD", "UNKNOWN_COMPANY", "VALUE_MISSING", "VALUE_SPREADSHEET_ERROR"]
    row = next(x for x in r.rows if x["company_code"] == "SVE")
    assert row["record_locator"] == "file=wcpr.csv;sheet=(csv);row=6" and row["sha256"] == "f" * 64
    assert not any(x["value"] == 0 for x in r.rows)


def test_wcpr_xlsx_finds_the_data_sheet_and_treats_uncached_formulas_as_missing(artifact_dir):
    path = write_wcpr_xlsx(artifact_dir / "wcpr.xlsx", header_row=5)
    r = parsers.parse_wcpr(str(path), _asset("wcpr.xlsx"), COMPANIES)
    assert len(r.rows) == 3
    formula = [x for x in r.rejects if "row=" in x["record_locator"] and x["reason_code"] == "VALUE_MISSING"]
    assert any("sheet=Data" in x["record_locator"] for x in formula)
    assert [s["sheet"] for s in r.evidence["sheets"]] == ["Data"]


def test_wcpr_conflicting_duplicates_are_quarantined(artifact_dir):
    rows = [
        ["Thames Water", "TMS", "APR3F_11", "Leakage", "3F.5", "2023-24", 590.1, "Ml/day"],
        ["Thames Water", "TMS", "APR3F_11", "Leakage", "3F.5", "2023-24", 591.0, "Ml/day"],
        ["Severn Trent Water", "SVE", "APR3F_11", "Leakage", "3F.5", "2023-24", 400.0, "Ml/day"],
        ["Severn Trent Water", "SVE", "APR3F_11", "Leakage", "3F.5", "2023-24", 400.0, "Ml/day"],
    ]
    path = write_wcpr_csv(artifact_dir / "dupes.csv", rows=rows)
    r = parsers.parse_wcpr(str(path), _asset("dupes.csv"), COMPANIES)
    assert [x["company_code"] for x in r.rows] == ["SVE"]
    assert [x["reason_code"] for x in r.rejects] == ["CONFLICTING_VALUES"] * 2


def test_wcpr_workbook_without_the_layout_fails_the_contract(artifact_dir):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["Company", "Leakage 2023-24"])
    save_workbook(wb, artifact_dir / "chart.xlsx")
    with pytest.raises(tabular.ContractError, match="no sheet matches"):
        parsers.parse_wcpr(str(artifact_dir / "chart.xlsx"), _asset("chart.xlsx"), COMPANIES)


# --- Stream ------------------------------------------------------------------------------------


def _stream_file(path, records):
    path.write_bytes(manifest.gzip_deterministic(manifest.canonical_records(records, "ObjectId")))
    return path


def _item(code="UUW", fy="2024-25", src_code=None):
    return registry.StreamItem(
        item_id="1" * 32,
        title="t",
        company_code=code,
        source_company_code=src_code or code,
        financial_year=fy,
        reporting_area="whole_company",
        item_type="feature_service",
        status="enabled",
        service_url="https://x",
        layer_id=0,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"with_item_ref": True, "line_key": "Line_Description", "separator": " - "},  # 2024-25 layout
        {"with_item_ref": True, "line_key": "Line_description", "separator": " - "},  # 2023-24 layout
        {"with_item_ref": False, "line_key": "Line_Description", "separator": " | "},  # 2025-26 layout
    ],
)
def test_stream_schema_drift_is_absorbed(artifact_dir, kwargs):
    recs = stream_records(5, **kwargs)
    path = _stream_file(artifact_dir / "records.jsonl.gz", recs)
    r = parsers.parse_stream(
        str(path), _asset("records.jsonl.gz", source_id="STREAM_APR", api_item_id="1" * 32), _item(), "4.13"
    )
    assert len(r.rows) == 5 and not r.rejects
    assert {x["attribute"] for x in r.rows} == {"Adjustments | Non-appointed"}
    assert r.rows[0]["record_locator"].startswith(f"api_item={'1' * 32};ObjectId=")
    assert (r.rows[0]["item_reference"] is not None) == kwargs["with_item_ref"]


def test_stream_company_and_period_conflicts_are_rejected(artifact_dir):
    recs = stream_records(3)
    recs[1]["Company"] = "SVE"
    recs[2]["Year"] = "2023-24"
    path = _stream_file(artifact_dir / "records.jsonl.gz", recs)
    r = parsers.parse_stream(
        str(path), _asset("records.jsonl.gz", source_id="STREAM_APR", api_item_id="1" * 32), _item(), None
    )
    assert len(r.rows) == 1
    assert sorted(x["reason_code"] for x in r.rejects) == ["COMPANY_CONFLICT", "PERIOD_CONFLICT"]


def test_stream_publisher_code_maps_to_canonical_company(artifact_dir):
    recs = stream_records(2, company="DCWW", year="2023/24")
    path = _stream_file(artifact_dir / "records.jsonl.gz", recs)
    r = parsers.parse_stream(
        str(path),
        _asset("records.jsonl.gz", source_id="STREAM_APR", api_item_id="1" * 32),
        _item(code="WSH", fy="2023-24", src_code="DCWW"),
        "4.12",
    )
    assert {x["company_code"] for x in r.rows} == {"WSH"} and {x["report_year"] for x in r.rows} == {2024}


def test_stream_csv_items_parse_with_row_locators(artifact_dir):
    import csv

    path = artifact_dir / "item.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Company",
                "Year",
                "Item reference",
                "Line description",
                "Units",
                "DPs",
                "RAG 4 reference",
                "Attribute",
                "Value",
            ]
        )
        w.writerow(["YKY", "2024-25", "BO1180STAT", "Revenue", "£m", "3", "1A.1", "Statutory", "1234.5"])
        w.writerow(["YKY", "2024-25", "BO1181STAT", "Opex", "£m", "3", "1A.2", "Statutory", "#N/A"])
    r = parsers.parse_stream(str(path), _asset("item.csv", source_id="STREAM_APR"), _item(code="YKY"), "4.13")
    assert [x["value"] for x in r.rows] == [1234.5] and r.rows[0]["record_locator"] == "file=item.csv;row=2"
    assert [x["reason_code"] for x in r.rejects] == ["VALUE_SPREADSHEET_ERROR"]


def test_attribute_separators():
    assert parsers.normalise_attribute("Adjustments - Non-appointed") == "Adjustments | Non-appointed"
    assert parsers.normalise_attribute("Adjustments | Non-appointed") == "Adjustments | Non-appointed"
    assert parsers.normalise_attribute("Statutory") == "Statutory"


def test_gzip_snapshot_round_trip(artifact_dir):
    recs = stream_records(3)
    path = _stream_file(artifact_dir / "r.jsonl.gz", recs)
    with gzip.open(path, "rt") as f:
        assert len(f.read().splitlines()) == 3
