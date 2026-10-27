# Data model

Schema names below are the defaults; yours may differ (see [deployment.md](deployment.md#variables)).

## Layers

| Layer | Schema | Contents | Who reads it |
|---|---|---|---|
| Landing | `water_performance_landing` | Volumes `raw`, `archive`, `curated_documents` | Pipeline; data owners upload to `raw/manual/` |
| Ops | `water_performance_ops` | Manifest, run log, rejects, quality results, releases, migrations, test results | Data owners, operators |
| Silver | `water_performance_silver` | One table per source, `conformed_observation`, release stores (`release_*`) | Pipeline only |
| Gold | `water_performance_gold` | Views and a Metric View over the published release | Analysts, Genie |

## Gold objects

Every Gold object reads the single release named in `ops.published_release`.

| Object | Grain | Purpose |
|---|---|---|
| `deployment_context` | exactly one row | The configured company: code, name, service type, jurisdiction and the sentence Genie uses to say how it read "we" |
| `dim_company` | company | Governed company list (`companies.csv`) |
| `dim_period` | financial year | `report_year` (year end), label `2024-25`, start and end dates, AMP and price review |
| `dim_source` | source | Registry entry: publisher, licence, attribution, status, precedence |
| `dim_metric` | metric | Canonical definition, unit, price base, direction, service scope, period, comparability and reason |
| `metric_source_map` | metric and source rule | The explicit crosswalk from source items to metrics |
| `fact_reported_performance` | source, company, reporting area, financial year, source item | Reported values from Ofwat WCPR and Stream APR, with comparability and provenance |
| `fact_performance_commitment` | price review, company, commitment, financial year | PR19 levels, actuals and PCL met; PR24 levels, deadbands and enhanced thresholds |
| `fact_odi` | price review, company, level, commitment or price control, financial year | PR19 ODI payments, GBP million, 2017-18 prices |
| `reconciliation` | metric, company, financial year | WCPR and Stream values for the same metric, with tolerance and verdict |
| `source_attribution` | source asset version | Attribution notice for everything in the release |
| `vw_reported_performance` | as `fact_reported_performance` | Facts joined to company, period, metric and source, with `is_configured_company` |
| `vw_peer_comparison` | company, metric, financial year | Comparable, preferred, whole-company values with rank, peer count and peer median |
| `vw_commitment_vs_actual` | as `fact_performance_commitment` | Targets against actuals, with the gap and `is_configured_company` |
| `vw_odi_position` | as `fact_odi` | ODI payments with company and `is_configured_company` |
| `vw_provenance` | any fact row | Publisher, release, locator, checksum, URLs, retrieval time and licence |
| `vw_source_coverage` | source | Status, availability in this release, hold reason, years and companies |
| `mv_peer_comparison` | Metric View | Measures `company_value`, `peer_median`, `best_rank`, `companies_compared`, `lowest_value`, `highest_value` |

## Periods

- `report_year` is the year a financial year ends: `2024-25` has `report_year = 2025`.
- `financial_year` is the label (`2024-25`). Bare years such as `2024` are never accepted from a
  source as financial years; a parser rejects them.
- AMP7 (PR19) is report years 2021 to 2025; AMP8 (PR24) is 2026 to 2030. PR19 and PR24
  commitments are separate rows with `price_review` set, and the quality gates fail a release
  that puts either outside its years.
- Calendar-year sources (Environment Agency, DWI) are on hold. When added, they will use a
  separate `calendar_year` and never be relabelled as financial years.
- Stream APR items sometimes repeat earlier years inside an attribute label, such as
  `Performance level | actual (2023-24)` in the 2024-25 report. `attribute_financial_year` records
  that year. Metric mappings use `{financial_year}` so that only the item's own year is mapped.

## Comparability

A row is comparable (`is_comparable = true`) only when all of these hold:

1. it maps to a metric in `dim_metric` with `comparability_status = 'comparable'`;
2. its published unit equals the unit approved in the metric's source rule;
3. it is a whole-company value (`reporting_area = 'whole_company'`), not a region;
4. a wastewater metric is not applied to a water-only company;
5. a metric defined for one regulatory period (for example AMP7) is used only in that period;
6. the metric records a price base (`not_applicable` for non-money measures).

When two sources report the same metric, company and year, the preferred value comes from Ofwat
WCPR, then Stream APR, then a company workbook (`is_preferred_source`). Both values are kept, and
`reconciliation` compares them: they agree when the difference is within half a unit of the
published decimal places, or 0.5 per cent where decimal places are not published. Disagreements
are reported as a warning, never averaged.

`comparability_reason` says why a row is not comparable, and Genie quotes it when asked to rank
such a measure.

## Releases

A refresh writes a candidate release under its run ID into every `silver.release_*` table. The
Gold views cannot see it. If every blocking quality gate passes, `finalize` replaces the single row
in `ops.published_release` with the new ID: one Delta commit switches every Gold object at once.
If a gate fails, the candidate is marked `rejected` and the previous release stays live.

`ops.release` lists every release and its status (`candidate`, `published`, `superseded`,
`rejected`, `rolled_back`). The release stores keep the five most recent published releases;
`rollback_release` can switch back to any of them.

## Provenance columns

Every Silver row and Gold fact row carries:

| Column | Meaning |
|---|---|
| `source_id` | Registry ID |
| `asset_key`, `asset_version` | Manifest key: asset or API item, and its version |
| `sha256` | Checksum of the archived bytes, or of the canonical API record set |
| `release_label` | The publisher's release name |
| `record_locator` | `file=...;sheet=...;row=...[;col=...]` or `api_item=...;ObjectId=...` |
| `parser_version` | Contract and parser version that produced the row |
| `run_id` | The refresh run (Silver) |

`ops.asset_manifest` adds the landing page, asset and final URLs, HTTP or API metadata, retrieval
time, byte size, licence, licence evidence and status for each asset version.
