# Bronze ingestion guide

This guide is for a team that downloads the source data with its own tools (for example Azure
Data Factory, or its own notebooks) instead of letting this project's job download it. It covers:

1. what to download, and from where;
2. how to save each file in bronze;
3. how to hand each file to this project, so its parsers build Silver and Gold.

**The full list of URLs is in [`source-urls.csv`](source-urls.csv)**, one row per file or API
dataset (64 rows). You can load it straight into a control table that drives your ingestion.

## How it fits together

```text
 Your ingestion tool                          This project (refresh_performance job)
 -------------------                          --------------------------------------
 1. download from the URL in source-urls.csv
 2. save it unchanged in your bronze
 3. copy it, with a small JSON sidecar, to
    /Volumes/<catalog>/<landing schema>/<raw volume>/manual/<source_id>/<asset_id>/
                                        ----> 4. inventory_archive: checks the checksum, archives it
                                              5. parse_<source>: the parser builds Silver
                                              6. conform, quality gates, publish Gold
```

You only own steps 1 to 3. Steps 4 to 6 are the same as for files the job downloads itself.

## The sources at a glance

| Source | What it is | Files | How to download | Saved as | Parser (in `bundles/data/src/water_performance/`) | Silver table |
|---|---|---:|---|---|---|---|
| `STREAM_APR` | Each company's APR tables on the Stream Water Data Portal, 2023-24 to 2025-26 | 51 (+1 on hold) | ArcGIS REST API: 49 feature services, 2 CSV items | `records.jsonl.gz` (feature service) or `.csv` | `stream_apr_v1`: `parsers.py` `parse_stream` | `stream_apr_record` |
| `COMPANY_APR` | Thames Water APR workbooks, 2020-21 to 2025-26 | 6 | Direct download | `.xlsx` as published | `company_apr_v1`: `parsers_apr.py` `parse_company_apr` | `company_apr_record` |
| `OFWAT_WCPR` | Ofwat Water Company Performance Report open data | 2 (+1 on hold) | Direct download | `.csv` / `.xlsx` as published | `ofwat_wcpr_normalised_v1`: `parsers.py` `parse_wcpr` | `ofwat_wcpr_record` |
| `OFWAT_PR19_ODI` | PR19 (2020-25) commitments, actuals and ODI payments | 1 | Direct download | `.xlsx` as published | `ofwat_pr19_odi_v1`: `parsers_outcomes.py` `parse_pr19` | `ofwat_pr19_pc`, `ofwat_pr19_price_control_odi` |
| `OFWAT_PR24_OUTCOMES` | PR24 (2025-30) performance commitment levels | 1 | Direct download | `.xlsx` as published | `ofwat_pr24_outcomes_v1`: `parsers_outcomes.py` `parse_pr24` | `ofwat_pr24_pc` |
| `RAG_DEFINITIONS` | Ofwat RAG 4.14 APR table definitions (a document, not numbers) | 1 | Direct download | `.pdf` as published | `rag_guideline_document_v1`: `tasks.py` `_rag_document` | `rag_guideline` |

Rows with `status = hold` in the CSV are listed for completeness. Skip them: the pipeline does not
load them, and the `notes` column says why.

## The CSV columns

| Column | Meaning |
|---|---|
| `source_id`, `asset_id` | The two names that identify the file. They are also the folder names in step 3. For Stream, `asset_id` is the ArcGIS item ID. |
| `title`, `company_code`, `financial_year` | What the file contains |
| `status` | `enabled` or `conditional`: load it. `hold`: skip it. |
| `method` | `direct_download`, `arcgis_feature_service_query` or `arcgis_item_download` (see step 1) |
| `download_url` | The exact URL to download |
| `licence_check_url` | Stream only: the item's metadata, which holds its licence |
| `landing_page` | The publisher's page for the file. Record it; you need it in the sidecar. |
| `save_as` | The file name and format to save |
| `bronze_folder` | The folder under the raw Volume to copy the file into (step 3) |
| `parser`, `parser_function`, `silver_table` | Which parser reads the file and where its rows go |
| `licence` | The licence ID to put in the sidecar |
| `notes` | Holds, licence overrides, and Stream items that cover only part of a company |

## Step 1: Download

### `direct_download` (Thames Water and Ofwat files)

Send an HTTP GET to `download_url` and save the response body exactly as received. Do not open
the file in Excel and save it again: that changes the checksum and can change the cells.

If the response is an HTML page instead of a file, the publisher is showing a browser check
(Ofwat does this sometimes). Do not work around it. Download that file by hand in a browser from
the `landing_page` instead.

### `arcgis_feature_service_query` (most Stream items)

Each Stream item is an ArcGIS table. Save it as `records.jsonl.gz`: one record per line, as
JSON, gzip-compressed.

1. Check the licence. GET `licence_check_url`. The item must have `"access": "public"`, and its
   `licenseInfo` must mention `creativecommons.org/licenses/by/4.0`. If it does not, stop and do
   not load it. The one exception is Southern Water 2024-25 (`ccd2a005a25e457caa6b85b929ebe9c3`),
   which is loaded by operator decision without an open licence (its `notes` say so).
2. Download every record, 1,000 at a time, in object ID order.
3. Check that the number of records matches what the service reports.

This is a complete example using only the Python standard library:

```python
import gzip
import json
import urllib.parse
import urllib.request


def get_json(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def snapshot_stream_item(item_id, download_url, out_path):
    """Save one Stream feature service as records.jsonl.gz. Returns the item metadata."""
    meta = get_json(f"https://www.arcgis.com/sharing/rest/content/items/{item_id}?f=json")
    if meta.get("access") != "public":
        raise ValueError(f"{item_id} is not public")

    layer = download_url.split("/query?")[0]  # .../FeatureServer/<layer_id>
    info = get_json(f"{layer}?f=json")
    oid = info.get("objectIdField") or "ObjectId"
    page = min(1000, info.get("maxRecordCount") or 1000)
    expected = get_json(f"{layer}/query?where=1%3D1&returnCountOnly=true&f=json")["count"]

    records, last = [], -1
    while True:
        q = urllib.parse.urlencode({
            "where": f"{oid} > {last}", "outFields": "*", "orderByFields": f"{oid} ASC",
            "resultRecordCount": page, "returnGeometry": "false", "f": "json",
        })
        batch = [f["attributes"] for f in get_json(f"{layer}/query?{q}")["features"]]
        if not batch:
            break
        records += batch
        last = max(r[oid] for r in batch)

    if len(records) != expected:
        raise ValueError(f"{item_id}: got {len(records)} records, the service reports {expected}")

    # mtime=0 and sorted keys: the same data always gives the same file and checksum.
    with gzip.GzipFile(out_path, "wb", mtime=0) as f:
        for r in records:
            f.write((json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    return meta
```

Keep the returned `meta`: the sidecar in step 3 uses its `licenseInfo` and `modified` fields.

### `arcgis_item_download` (two Yorkshire Water Stream items)

Check the licence as above, then GET `download_url` and save the CSV exactly as received.

## Step 2: Save in bronze

Save each file unchanged, in whatever layout your platform uses. We suggest:

```text
<bronze root>/<source_id>/<asset_id>/<download date>/<file>
```

Keep these facts for each download, because step 3 needs them: the download URL, the landing
page, the time of download, the SHA-256 of the file, and the person or team who ran it.

## Step 3: Hand the file to this project

Copy the file to the raw Volume of the deployed data bundle, in the folder given by
`bronze_folder`:

```text
/Volumes/<catalog>/<landing schema>/<raw volume>/manual/<source_id>/<asset_id>/<file>
```

The default landing schema is `water_performance_landing` and the default raw Volume is `raw`.

Next to the file, write a sidecar called `<file>.bootstrap.json`. For example, for
`records.jsonl.gz` the sidecar is `records.jsonl.gz.bootstrap.json`.

| Sidecar field | Required | Value |
|---|---|---|
| `source_id` | Yes | From the CSV |
| `asset_id` | Yes | From the CSV |
| `asset_url` | Yes | `download_url` from the CSV (must start with `https://`) |
| `landing_page` | Yes | `landing_page` from the CSV (must start with `https://`) |
| `release` | Yes | A readable name, for example `title` from the CSV |
| `published` | Yes | The publication date if known, else the download date (`YYYY-MM-DD`) |
| `licence_id` | Yes | `licence` from the CSV: `OGL-UK-3.0`, `CC-BY-4.0` or `NO-OPEN-LICENCE` |
| `licence_evidence` | Yes | Where the licence is stated, in one sentence |
| `supplied_by` | Yes | The person or team who downloaded it |
| `downloaded_at` | Yes | ISO 8601 time, for example `2026-11-17T09:30:00Z` |
| `sha256` | Yes | SHA-256 of the file exactly as copied (for Stream, of the `.gz` file) |
| `item_id` | Stream only | The item ID (same as `asset_id`) |
| `licence_text` | Stream only | The item's `licenseInfo` text |
| `rag_version` | Stream, optional | The RAG version named in `licenseInfo`, for example `4.13` |
| `licence_override` | Southern Water 2024-25 only | The reason it is loaded without an open licence (copy the CSV `notes`) |

Example for an Ofwat file:

```json
{
  "source_id": "OFWAT_WCPR",
  "asset_id": "wcpr_2024_25_normalised_post_fd_xlsx",
  "asset_url": "https://www.ofwat.gov.uk/wp-content/uploads/2025/10/WCPR_2024-25-Open-Data-Machine-Readable-Normalised-PostFDUpdate.xlsx",
  "landing_page": "https://www.ofwat.gov.uk/publication/data-for-the-water-company-performance-report-2024-25-open-data-friendly-machine-readable-format/",
  "release": "2024-25 post-final-determination update",
  "published": "2025-10-23",
  "licence_id": "OGL-UK-3.0",
  "licence_evidence": "Ofwat states that WCPR data is released under the Open Government Licence v3.0 on the landing page.",
  "supplied_by": "Thames Water data platform team",
  "downloaded_at": "2026-11-17T09:30:00Z",
  "sha256": "<sha256 of the file>"
}
```

Example for a Thames Water workbook:

```json
{
  "source_id": "COMPANY_APR",
  "asset_id": "tms_apr_tables_2025_26",
  "asset_url": "https://www.thameswater.co.uk/media-library/dvrggetx/2025-26-apr-tables.xlsx",
  "landing_page": "https://www.thameswater.co.uk/about-us/performance/regulatory-library",
  "release": "Thames Water APR tables 2025-26",
  "published": "<publication date, YYYY-MM-DD>",
  "licence_id": "NO-OPEN-LICENCE",
  "licence_evidence": "Thames Water's own published APR tables, supplied by Thames Water.",
  "supplied_by": "Thames Water data platform team",
  "downloaded_at": "2026-11-17T09:30:00Z",
  "sha256": "<sha256 of the file>"
}
```

Example for a Stream item:

```json
{
  "source_id": "STREAM_APR",
  "asset_id": "e0fbbbae41f24b29a362d3c73ef356ba",
  "item_id": "e0fbbbae41f24b29a362d3c73ef356ba",
  "asset_url": "<download_url from the CSV>",
  "landing_page": "https://www.streamwaterdata.co.uk/datasets/e0fbbbae41f24b29a362d3c73ef356ba",
  "release": "United Utilities APR 2024-25 (Stream)",
  "published": "<the item's modified date, YYYY-MM-DD>",
  "licence_id": "CC-BY-4.0",
  "licence_evidence": "Stream item licenseInfo links to creativecommons.org/licenses/by/4.0.",
  "licence_text": "<the item's licenseInfo text>",
  "rag_version": "<RAG version named in licenseInfo, if any>",
  "supplied_by": "Thames Water data platform team",
  "downloaded_at": "2026-11-17T09:30:00Z",
  "sha256": "<sha256 of records.jsonl.gz>"
}
```

The `licence_id` must match the CSV. If it does not, the file is archived but held back
(`quarantined`) and never reaches Gold.

## Step 4: Run the job and check it worked

Run the `refresh_performance` job. Then check:

```sql
-- Each file you copied should appear here as 'current'
SELECT source_id, asset_key, asset_version, status, status_reason, retrieval_mode, retrieved_at
FROM <catalog>.water_performance_ops.asset_manifest
WHERE retrieval_mode = 'manual_bootstrap'
ORDER BY source_id, asset_key;

-- Any sidecar problems (missing field, wrong checksum, wrong folder)
SELECT source_id, record_locator, reason_code, reason
FROM <catalog>.water_performance_ops.reject
WHERE reason_code IN ('BOOTSTRAP_METADATA', 'BOOTSTRAP_LAYOUT', 'UNKNOWN_SOURCE', 'CONTRACT_MISMATCH')
ORDER BY rejected_at DESC;

-- What each parser produced
SELECT source_id, asset_key, status, rows_out, rows_rejected, message
FROM <catalog>.water_performance_ops.parse_log
ORDER BY parsed_at DESC;
```

## When a publisher releases a new version

- Download it and copy it in with a **new file name** (for example, add the download date).
  Files in the raw Volume are never overwritten.
- Leave the old file where it is. The pipeline recognises it by checksum and does not load it
  again.
- If the content is identical to a version already loaded, nothing changes.
- If the file is a new asset that is not in the CSV (for example a 2026-27 workbook), it must be
  registered first in `sources.yml` or `stream_apr_items.yml`. See
  [adding-a-source.md](adding-a-source.md), then run `python3 tools/export_source_urls.py` to
  refresh the CSV.

## Things to know

- **Do not change the files.** The parsers find tables by their content, but a re-saved workbook
  can lose values and always changes the checksum.
- **The job also tries to download.** `refresh_performance` still runs its own `fetch_*` tasks.
  If your workspace cannot reach the publishers' websites, those tasks fail. Your copied files
  are still loaded and Gold is still published if its checks pass, but the run is marked failed.
  To stop this, remove the `fetch_*` and `discover_sources` tasks from
  `bundles/data/resources/refresh_performance.job.yml` (and from the `depends_on` list of
  `inventory_archive`), then redeploy.
- **Stream items differ by year.** Column names change between years (for example
  `Line_description` and `Line_Description`). The Stream parser handles this. Save the records
  exactly as the API returns them.
- **Some Stream items cover part of a company.** South West Water publishes its Bristol and
  South West areas separately. Load them all: the pipeline keeps them apart and does not use them
  for peer comparison.
- **Your sidecar is the licence confirmation for the Ofwat files.** When the job downloads PR19,
  PR24 or the RAG guideline itself, it loads them only if it finds an Open Government Licence
  statement inside the file (PR24 Key Dataset 1 has none, so it stays held back). A file you supply
  with a sidecar is loaded on the strength of your `licence_id` and `licence_evidence`. Only write
  `OGL-UK-3.0` once you have seen the Open Government Licence statement on the landing page.
- **Keeping the CSV up to date.** The CSV is generated from
  `bundles/data/src/water_performance/config/sources.yml` and `stream_apr_items.yml`. Run
  `python3 tools/export_source_urls.py` after either file changes.
