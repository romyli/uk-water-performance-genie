# Source audit

This page explains how the project decides which public data it may use, how each version of a
source is identified, and how to trace any published number back to the publisher's asset.

The human source register is [sources.md](../sources.md). The machine-readable registry that the
jobs read is `bundles/data/src/water_performance/config/sources.yml`, with the pinned Stream items
in `stream_apr_items.yml` next to it. The validation job fails if the two registers disagree on
source IDs or priorities.

## When a source is accepted

A source is ingested only when all of these are recorded:

| Requirement | Where it is recorded |
|---|---|
| Publisher and authoritative landing page | `publisher`, `landing_page` (source and each asset) |
| Jurisdiction | `jurisdiction`; only `england_wales`, `england` or `wales` can be enabled in this release |
| Retrieval route | `retrieval_mode`: `api`, `direct_asset` or `manual_bootstrap` |
| Open licence that allows redistribution | `licence.id` (CC BY 4.0 or OGL v3.0) and `licence.evidence` |
| Attribution text | `attribution` |
| Release identity | asset `release` and `published`, or the API item ID and edit time |
| Reporting basis | `reporting_basis` (financial or calendar year, definitions version) |
| Parser contract | `contract` and `contract_version` |

Status values:

- `enabled`: every requirement is met and the source is ingested.
- `conditional`: the adapter exists, but each asset version is used only after its licence is
  verified, either from an Open Government Licence statement found inside the asset or from a
  licence the operator recorded when supplying it through manual bootstrap.
- `hold`: not ingested. `hold_reason` says what is missing.

Public visibility is not a licence. A dashboard without a supported export, or a download without
stated reuse terms, can be linked and cited but is never loaded as data.

## Current status

| Source | Status | Licence | Why |
|---|---|---|---|
| `OFWAT_WCPR` | enabled | OGL v3.0 | Ofwat releases Water Company Performance Report data under OGL v3.0 |
| `STREAM_APR` | enabled | CC BY 4.0, checked per item | Each item's licence text is read on every retrieval |
| `OFWAT_PR24_OUTCOMES` | conditional | OGL v3.0 when stated in the asset | Landing page cannot be read automatically |
| `OFWAT_PR19_ODI` | conditional | OGL v3.0 when stated in the asset | Landing page cannot be read automatically |
| `RAG_DEFINITIONS` | conditional | OGL v3.0 when stated in the document | Reference metadata, not observations |
| `COMPANY_APR` | hold | not recorded | No company workbook has recorded reuse terms |
| P1 and P2 sources | hold | see [sources.md](../sources.md) | Bulk access, schema or licence not yet confirmed |

The decisions behind these statuses are in [data-issues-and-decisions.md](data-issues-and-decisions.md).
To see the status a particular run used, query `ops.source_registry_snapshot`.

## Releases and versions

Every retrieved file or API snapshot becomes a row in `ops.asset_manifest`, keyed by
`source_id`, `asset_key` and `asset_version`.

- **Files** (`direct_asset`, `manual_bootstrap`): the identity is the SHA-256 of the exact bytes
  received. The manifest also keeps the requested and final URL, HTTP status, `ETag`,
  `Last-Modified`, content type, byte size and retrieval time.
- **API snapshots** (`api`): every record is fetched by object ID, the full set is written as
  JSON Lines ordered by object ID, and the identity is the SHA-256 of that canonical text. The
  manifest keeps the ArcGIS item ID, service URL, layer, record count, page count, the item's
  modified time and the layer's data edit time.

A rerun with unchanged content records a check but creates no new version. Changed content
becomes the next version; the previous one is marked `superseded` and stays queryable. An asset
that is removed from the registry is marked `withdrawn`. Nothing is deleted. Archived copies sit
in the archive Volume under `<source_id>/<asset_key>/sha256=<checksum>/`, so a copy can never
be silently replaced.

A failed or blocked retrieval does not touch the archive. The previous version stays current and
the last published Gold release stays live.

## Schema contracts

Each parser has a contract: the logical columns it needs and the header labels each may appear
under. The parser finds the header row by content in every sheet, so moved rows and renamed
sheets do not break it, and an unrecognised layout is rejected with the rows it saw as evidence.

Values are read with these rules:

- spreadsheet errors (`#N/A`, `#DIV/0!` and similar), placeholders (`-`, `n/a`, `x`) and empty
  cells are not loaded and are never treated as zero;
- formulas without a cached result are treated as missing;
- two rows with the same key but different values are both quarantined.

Every rejected value is written to `ops.reject` with its locator and reason code. Stream schemas
differ by year (field casing, a missing `Item_reference` field, a different attribute separator);
the Stream contract accepts each known variant and records the field names it matched.

## Attribution

Each published release writes `gold.source_attribution` from the manifest: one row per source
asset version used, with the licence, attribution text and URLs. To produce the notice for a
report or application:

```sql
SELECT DISTINCT source_id, attribution_text, licence_name, licence_url, landing_page
FROM <catalog>.<gold_schema>.source_attribution
ORDER BY source_id;
```

Stream items are licensed CC BY 4.0 by each company; Ofwat material is Crown copyright under the
Open Government Licence v3.0. Keep any third-party-rights exception shown in the
`third_party_rights` column.

## Tracing a number

Every row in `gold.fact_reported_performance` carries its source, asset version, checksum and
record locator. `gold.vw_provenance` joins those to the manifest. Replace the placeholders and
run this in a SQL editor:

```sql
SELECT company_code, financial_year, original_label, value, unit,
       source_id, release_label, record_locator, sha256, api_item_id,
       asset_url, landing_page, retrieved_at, licence_id, archive_path
FROM <catalog>.<gold_schema>.vw_provenance
WHERE company_code = '<company code>'
  AND metric_id = '<metric id>'
  AND financial_year = '<yyyy-yy>';
```

What each column proves:

- `record_locator` is where the value sits in the asset: `file=...;sheet=...;row=...` for a file,
  or `api_item=<item id>;ObjectId=<n>` for an API record.
- `sha256` identifies the exact bytes (files) or canonical records (API snapshots). Recompute it
  from `archive_path` to prove the copy is unchanged.
- `asset_url` and `landing_page` are the publisher's public locations; `licence_id` is the
  licence the gate accepted.

### Worked example

Thames Water's per capita consumption for 2023-24, traced from a published release:

| Step | Value |
|---|---|
| Gold value | 138.2 litres per person per day (`metric_id = per_capita_consumption`, comparable) |
| Original label | Per capita consumption (PCC) - lpd (standardised performance level, annual figure) |
| Source and release | `OFWAT_WCPR`, 2024-25 post-final-determination update |
| Record locator | `file=WCPR_2024-25-Open-Data-Machine-Readable-Normalised-PostFDUpdate.xlsx;sheet=in;row=24637` |
| SHA-256 | `62fe9156388f76c04e36cd494ba927124dc7f239928dc0f7e1e257b91168976b` |
| Asset | https://www.ofwat.gov.uk/wp-content/uploads/2025/10/WCPR_2024-25-Open-Data-Machine-Readable-Normalised-PostFDUpdate.xlsx |
| Landing page | https://www.ofwat.gov.uk/publication/data-for-the-water-company-performance-report-2024-25-open-data-friendly-machine-readable-format/ |
| Licence | Open Government Licence v3.0 |

The 2023-24 release also reports this year. Because the 2024-25 release is later, it supplies the
value; if the two differed, `restated` would be true and `previous_release_value` would hold the
earlier figure. To check the archived copy, download the asset URL and compare its SHA-256 with
the one above.

## Coverage gaps

- Thames Water has no Stream APR item. Its detailed APR tables are not available until a company
  workbook with recorded reuse terms is registered; its WCPR and Ofwat outcome data are available.
- Southern Water 2024-25 APR data is not loaded (licence "N/A" on the Stream item).
- Stream items start in 2023-24. Earlier detailed APR tables need company workbooks.
- Environment Agency, Drinking Water Inspectorate, CCW and boundary data are on hold.
- Scotland and Northern Ireland are not covered.
