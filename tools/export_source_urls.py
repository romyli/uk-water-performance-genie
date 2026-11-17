#!/usr/bin/env python3
"""Write docs/source-urls.csv: every source URL the pipeline downloads, and the parser that reads it.

The CSV is generated from the two registry files, so it never disagrees with what the pipeline loads:

    bundles/data/src/water_performance/config/sources.yml          (Ofwat, company APR, RAG)
    bundles/data/src/water_performance/config/stream_apr_items.yml (Stream APR items)

Run from the repository root after changing either file:

    python3 tools/export_source_urls.py

See docs/bronze-ingestion-guide.md for how to use the CSV.
"""

from __future__ import annotations

import csv
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "bundles" / "data" / "src" / "water_performance" / "config"
OUT = ROOT / "docs" / "source-urls.csv"

# contract -> (parser function, Silver table). Matches tasks.parse and tasks.SILVER_FOR.
PARSERS = {
    "stream_apr_v1": ("parsers.py:parse_stream", "stream_apr_record"),
    "company_apr_v1": ("parsers_apr.py:parse_company_apr", "company_apr_record"),
    "ofwat_wcpr_normalised_v1": ("parsers.py:parse_wcpr", "ofwat_wcpr_record"),
    "ofwat_pr19_odi_v1": ("parsers_outcomes.py:parse_pr19", "ofwat_pr19_pc, ofwat_pr19_price_control_odi"),
    "ofwat_pr24_outcomes_v1": ("parsers_outcomes.py:parse_pr24", "ofwat_pr24_pc"),
    "rag_guideline_document_v1": ("tasks.py:_rag_document", "rag_guideline"),
}

# Assets that cover several years in one file.
MULTI_YEAR = {
    "OFWAT_PR19_ODI": "2020-21 to 2024-25",
    "OFWAT_PR24_OUTCOMES": "2025-26 to 2029-30",
    "RAG_DEFINITIONS": "all (reference document)",
}

COLUMNS = [
    "source_id",
    "asset_id",
    "title",
    "company_code",
    "financial_year",
    "status",
    "method",
    "download_url",
    "licence_check_url",
    "landing_page",
    "save_as",
    "bronze_folder",
    "parser",
    "parser_function",
    "silver_table",
    "licence",
    "notes",
]


def load(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))


def parser_cols(contract: str) -> dict:
    fn, table = PARSERS[contract]
    return {"parser": contract, "parser_function": f"bundles/data/src/water_performance/{fn}", "silver_table": table}


def stream_rows(source: dict, items: list[dict]) -> list[dict]:
    api = source["api"]
    rows = []
    for it in items:
        item_id = it["item_id"]
        if it["item_type"] == "csv":
            method = "arcgis_item_download"
            url = api["item_data_url"].format(item_id=item_id)
            save_as = "<file name from the Content-Disposition header>.csv"
        else:
            method = "arcgis_feature_service_query"
            layer = f"{it['service_url']}/{it['layer_id']}"
            url = f"{layer}/query?where=1%3D1&outFields=*&returnGeometry=false&f=json"
            save_as = "records.jsonl.gz"
        notes = []
        if it.get("hold_reason"):
            notes.append(f"On hold: {it['hold_reason']}")
        if it.get("licence_override"):
            notes.append(f"Licence override: {it['licence_override']}")
        if it.get("reporting_area", "whole_company") != "whole_company":
            notes.append(f"Reporting area: {it['reporting_area']} (not the whole company)")
        rows.append(
            {
                "source_id": "STREAM_APR",
                "asset_id": item_id,
                "title": it["title"],
                "company_code": it["company_code"],
                "financial_year": it["financial_year"],
                "status": it.get("status", "enabled"),
                "method": method,
                "download_url": url,
                "licence_check_url": api["item_metadata_url"].format(item_id=item_id),
                "landing_page": f"https://www.streamwaterdata.co.uk/datasets/{item_id}",
                "save_as": save_as,
                "bronze_folder": f"manual/STREAM_APR/{item_id}/",
                **parser_cols(source["contract"]),
                "licence": "NO-OPEN-LICENCE" if it.get("licence_override") else "CC-BY-4.0 (checked per item)",
                "notes": " ".join(notes),
            }
        )
    return rows


def asset_rows(source: dict) -> list[dict]:
    rows = []
    for a in source.get("assets") or []:
        notes = []
        if a.get("hold_reason"):
            notes.append(f"On hold: {a['hold_reason']}")
        if a.get("supersedes"):
            notes.append(f"Replaces {a['supersedes']}.")
        if a.get("licence_verification"):
            notes.append(f"Licence check: {a['licence_verification']}.")
        rows.append(
            {
                "source_id": source["source_id"],
                "asset_id": a["asset_id"],
                "title": a["release"],
                "company_code": a.get("company_code", "ALL"),
                "financial_year": a.get("financial_year")
                or (f"{a['report_year_to'] - 1}-{str(a['report_year_to'])[2:]}" if a.get("report_year_to") else "")
                or MULTI_YEAR.get(source["source_id"], ""),
                "status": a.get("status", source["status"]),
                "method": "direct_download",
                "download_url": a["url"],
                "licence_check_url": "",
                "landing_page": a["landing_page"],
                "save_as": urllib.parse.unquote(a["url"].rsplit("/", 1)[-1]),
                "bronze_folder": f"manual/{source['source_id']}/{a['asset_id']}/",
                **parser_cols(source["contract"]),
                "licence": source["licence"]["id"],
                "notes": " ".join(notes),
            }
        )
    return rows


def main() -> None:
    sources = load("sources.yml")["sources"]
    items = load("stream_apr_items.yml")["items"]
    rows = []
    for s in sources:
        if s["status"] == "hold" or not s.get("contract"):
            continue  # not loaded; sources.md explains why
        if s["source_id"] == "STREAM_APR":
            rows += stream_rows(s, items)
        else:
            rows += asset_rows(s)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
