"""Test doubles and fixture generators. Nothing here touches a publisher."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable

from water_performance.http import HttpClient


class FakeTransport:
    """Serves canned responses by URL. `routes` maps a predicate to a response function."""

    def __init__(self):
        self.routes: list[tuple[Callable[[str], bool], Callable[[str], tuple]]] = []
        self.calls: list[str] = []

    def on(self, predicate, responder):
        self.routes.append((predicate, responder))
        return self

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        for pred, resp in self.routes:
            if pred(url):
                return resp(url)
        return 404, url, {"content-type": "text/plain"}, b"not found"


def json_response(payload, status=200, headers=None):
    return lambda url: (
        status,
        url,
        {"content-type": "application/json", **(headers or {})},
        json.dumps(payload).encode(),
    )


def client(transport) -> HttpClient:
    return HttpClient(transport=transport, sleep=lambda s: None, min_interval_seconds=0, backoff_seconds=0)


class FakeArcGIS:
    """A FeatureServer table with `n` records that honours `where=<oid> > N` keyset pagination."""

    def __init__(self, records: list[dict], oid="ObjectId", max_records=3, licence_html=None, modified=1700000000000):
        self.records = records
        self.oid = oid
        self.max_records = max_records
        self.licence_html = (
            licence_html
            if licence_html is not None
            else (
                "<p>Produced in accordance with Ofwat's Regulatory Accounting Guidance (RAG) 4.13. "
                "Licensed under <a href='https://creativecommons.org/licenses/by/4.0/'>CC BY 4.0</a></p>"
            )
        )
        self.modified = modified
        self.queries = 0

    def install(self, t: FakeTransport, item_id: str, service: str):
        t.on(
            lambda u: u.startswith(f"https://www.arcgis.com/sharing/rest/content/items/{item_id}?"),
            json_response(
                {
                    "id": item_id,
                    "title": "Test APR",
                    "access": "public",
                    "modified": self.modified,
                    "owner": "test",
                    "licenseInfo": self.licence_html,
                }
            ),
        )
        t.on(
            lambda u: u == f"{service}/0?f=json",
            json_response(
                {
                    "name": "t",
                    "objectIdField": self.oid,
                    "maxRecordCount": self.max_records,
                    "editingInfo": {"dataLastEditDate": 1700000001000, "schemaLastEditDate": 1700000000500},
                    "fields": [
                        {"name": k, "type": "esriFieldTypeOID" if k == self.oid else "esriFieldTypeString"}
                        for k in self.records[0]
                    ],
                }
            ),
        )
        t.on(
            lambda u: u.startswith(f"{service}/0/query") and "returnCountOnly=true" in u,
            json_response({"count": len(self.records)}),
        )
        t.on(lambda u: u.startswith(f"{service}/0/query"), self._page)
        return self

    def _page(self, url):
        self.queries += 1
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        where = q["where"][0]
        last = int(where.split(">")[1])
        n = int(q["resultRecordCount"][0])
        rows = sorted((r for r in self.records if r[self.oid] > last), key=lambda r: r[self.oid])[:n]
        return (
            200,
            url,
            {"content-type": "application/json"},
            json.dumps(
                {"features": [{"attributes": r} for r in rows], "exceededTransferLimit": len(rows) == n}
            ).encode(),
        )


def stream_records(
    n=7, company="UUW", year="2024-25", with_item_ref=True, line_key="Line_Description", separator=" - "
):
    rows = []
    for i in range(1, n + 1):
        r = {
            "Company": company,
            "Year": year,
            line_key: f"Line {i}",
            "Units": "£m",
            "DPs": 3,
            "RAG_4_reference": f"1A.{i}",
            "Attribute": f"Adjustments{separator}Non-appointed",
            "Value": float(i),
            "ObjectId": i,
        }
        if with_item_ref:
            r["Item_reference"] = f"BO{i:04d}"
        rows.append(r)
    return rows


WCPR_HEADER = [
    "COMPANY_NAME",
    "COMPANY_ABBREVIATION",
    "UNIQUE_ITEM_REFERENCE",
    "SHORT_DESCRIPTION",
    "RAG",
    "YEAR",
    "VALUE",
    "UNITS",
]


def wcpr_rows():
    return [
        [
            "Thames Water",
            "TMS",
            "APR3F_11",
            "Leakage - actual performance level (annual figure)",
            "3F.5",
            "2023-24",
            590.1,
            "Ml/day",
        ],
        [
            "Thames Water",
            "TMS",
            "APR3F_11",
            "Leakage - actual performance level (annual figure)",
            "3F.5",
            "2022-23",
            610.0,
            "Ml/day",
        ],
        [
            "Severn Trent Water",
            "SVE",
            "APR3F_11",
            "Leakage - actual performance level (annual figure)",
            "3F.5",
            "2023-24",
            400.5,
            "Ml/day",
        ],
        ["Severn Trent Water", "SVE", "APR3F_10", "Per capita consumption", "3F.6", "2023-24", "#N/A", "lpd"],
        ["Severn Trent Water", "SVE", "APR3F_02", "Household population", "3F.4", "2023-24", "", "000s"],
        ["Atlantis Water", "ATL", "APR3F_11", "Leakage", "3F.5", "2023-24", 1.0, "Ml/day"],
        ["Thames Water", "TMS", "BN1100", "Mains length", "6C.1", "2023", 1.0, "km"],
    ]


def write_wcpr_csv(path, header_offset=0, rows=None):
    import csv

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for _ in range(header_offset):
            w.writerow(["Ofwat open data", "", ""])
        w.writerow(WCPR_HEADER)
        w.writerows(rows if rows is not None else wcpr_rows())
    return path


def write_wcpr_xlsx(path, header_row=4, rows=None, extra_sheet=True, with_formula=True):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws["A1"] = "Contains public sector information licensed under the Open Government Licence v3.0."
    data = wb.create_sheet("Data")
    for _ in range(header_row - 1):
        data.append(["", "Water company performance report"])
    data.append(WCPR_HEADER)
    for r in rows if rows is not None else wcpr_rows():
        data.append(r)
    if with_formula:
        # A formula without a cached value (openpyxl never computes one): must not become zero.
        data.append(["Thames Water", "TMS", "APR3F_99", "Formula line", "3F.9", "2023-24", "=1+1", "nr"])
    if extra_sheet:
        wb.create_sheet("Chart data").append(["unrelated", "table"])
    return save_workbook(wb, path)


def save_workbook(wb, path):
    """Save locally, then copy: Volume paths do not support the random writes a zip file needs."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        local = f"{tmp}/book.xlsx"
        wb.save(local)
        shutil.copyfile(local, path)
    return path


def _put(ws, row, cells: dict) -> None:
    """Write {zero-based column: value} into one 1-based worksheet row."""
    for col, value in cells.items():
        ws.cell(row=row, column=col + 1, value=value)


def write_company_apr_xlsx(path, company="Thames Water", year_end=2025, duplicate_heading=False, second_unit="000s"):
    """A company APR workbook on Ofwat's pro forma template: a cover sheet, an income statement with
    merged headings and an Ofwat Bon Numbers grid, a table whose units and DPs are header rows, a
    table that changes its headings part-way down, and a debt-instrument register."""
    import openpyxl

    wb = openpyxl.Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["B2"] = f"{year_end - 1}-{str(year_end)[-2:]} APR tables"

    ws = wb.create_sheet("1A")
    _put(ws, 1, {1: "Pro forma 1A", 23: "Ofwat Bon Numbers"})
    _put(ws, 2, {1: company})
    _put(ws, 3, {1: f"Income statement for the 12 months ended 31 March {year_end}"})
    _put(ws, 5, {1: "Line description", 3: "Units", 4: "DPs", 5: "Statutory", 6: "Adjustments"})
    _put(ws, 5, {9: "Total appointed activities", 11: "RAG 4 reference", 13: "Comments (For internal use only)"})
    _put(ws, 5, {23: "Line description", 25: "Statutory", 26: "Adjustments", 29: "Total appointed activities"})
    subs = ("Differences between statutory and RAG definitions", "Non-appointed", "Total adjustments")
    _put(ws, 6, {0: "Header", 6: subs[0], 7: subs[1], 8: subs[2], 26: subs[0], 27: subs[1], 28: subs[2]})
    _put(ws, 7, {0: "Start_Data", 25: "Start_Bon"})
    _put(ws, 8, {1: "Revenue", 3: "£m", 4: 3, 5: 2738.2, 6: 40.043, 7: 157.162, 8: -117.119, 9: 2621.081})
    _put(ws, 8, {11: "1A.1", 13: "not a value", 23: "Revenue", 25: "BO1180STAT", 26: "BO1180DSR"})
    _put(ws, 8, {27: "BO1180NA", 28: "BO1180ADJ", 29: "BO1180"})
    _put(ws, 9, {1: "Operating profit", 3: "£m", 4: 3, 5: -799.3, 6: "#REF!", 7: -1123.461, 9: 208.506})
    _put(ws, 9, {11: "1A.4", 23: "Operating profit", 25: "BO2060STAT", 26: "Copy", 27: "BO2060NA", 29: "BO2060"})

    rr = wb.create_sheet("2F")
    _put(rr, 1, {1: "Pro forma 2F"})
    _put(rr, 2, {1: company})
    _put(rr, 3, {1: f"Residential retail for the 12 months ended 31 March {year_end}"})
    heading = "Revenue" if duplicate_heading else "Number of customers"
    _put(rr, 5, {1: "Line description", 2: "Revenue", 3: heading, 6: "RAG 4 reference"})
    _put(rr, 6, {1: "Units", 2: "£m", 3: second_unit})
    _put(rr, 7, {1: "DPs", 2: 3, 3: 3})
    _put(rr, 9, {1: "Residential revenue", 2: 1500.25, 3: 6000.5, 6: "2F.1"})

    cp = wb.create_sheet("4R")
    _put(cp, 1, {1: "Pro forma 4R"})
    _put(cp, 2, {1: company})
    _put(cp, 5, {1: "Line description", 2: "Units", 3: "DPs", 4: "Measured", 5: "Total", 7: "RAG 4 reference"})
    _put(cp, 7, {1: "Residential water only customers", 2: "000s", 3: 3, 4: 26.449, 5: 50.887, 7: "4R.1"})
    _put(cp, 9, {4: "Water", 6: "Wastewater"})
    _put(cp, 10, {4: "Measured", 5: "Total", 6: "Measured"})
    _put(cp, 11, {1: "Connected properties", 2: "000s", 3: 3, 4: 3000.1, 5: 4000.2, 6: 2500.3, 7: "4R.10"})

    debt = wb.create_sheet("4B")
    _put(debt, 1, {1: "Pro forma 4B"})
    _put(debt, 2, {1: company})
    _put(debt, 5, {1: "Line description", 2: "Units", 3: "DPs", 4: "Instrument identifier (e.g. ISIN)"})
    _put(debt, 5, {6: "RAG 4 reference"})
    _put(debt, 7, {1: "Bond 1", 2: "£m", 3: 3, 4: 12.5, 6: "4B.1"})
    return save_workbook(wb, path)
