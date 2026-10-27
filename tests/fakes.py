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
