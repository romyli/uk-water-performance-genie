# Adding sources, releases, metrics, companies and benchmarks

Every change below is configuration or code in this repository, tested by `validate_project` and
applied by a data bundle deploy and a refresh. None of them needs a Genie redeploy unless the
section says so.

Configuration lives in `bundles/data/src/water_performance/config/`:

| File | Holds |
|---|---|
| `sources.yml` | The machine source registry: one entry per source, with assets for direct downloads |
| `stream_apr_items.yml` | The Stream APR items that may be ingested |
| `companies.csv` | The governed company list |
| `reporting_areas.csv` | Publisher codes for regions or alternative company codes |
| `metrics.yml` | The metric crosswalk and comparability rules |
| `quality.yml` | Completeness, freshness and retention thresholds |

## A new release of an existing source

**Ofwat file (for example the next WCPR open data):** add an asset under the source in
`sources.yml`:

```yaml
      - asset_id: wcpr_2025_26_normalised_csv
        release: 2025-26 (October 2026)
        report_year_to: 2026
        published: 2026-10-22
        url: https://www.ofwat.gov.uk/wp-content/uploads/<path to the asset>
        landing_page: https://www.ofwat.gov.uk/publication/<publication page>/
```

`report_year_to` orders overlapping releases: where two releases report the same company, item
and year, the later release supplies the value and the earlier value is kept as
`previous_release_value` with `restated = true` if it changed. Use `supersedes: <asset_id>` when
the publisher replaces a file in place.

**Stream item:** the `discover_sources` task lists new public items in `ops.source_candidate`
with `registered = false`. Read the item's licence on Stream, then add it to
`stream_apr_items.yml` with its company code, the code used inside the data
(`source_company_code`), financial year, reporting area, service URL and layer ID. The licence is
still checked from the item on every retrieval.

Also add the new release to [sources.md](../sources.md) if it changes the register's coverage notes.

## A new source

1. Add a row to the core table in [sources.md](../sources.md) with an ID, priority, publisher,
   coverage, the questions it enables and its access and licence notes.
2. Add the entry to `sources.yml` with every field in [source-audit.md](source-audit.md#when-a-source-is-accepted).
   Start with `status: hold` and a `hold_reason`.
3. Write a parser with a contract: the logical columns, the labels each can appear under, and how
   values are read. Put it in `water_performance/parsers.py` (or its own module), and add a
   Silver table in a new numbered migration in `migrations.py`. Parsers must not depend on file
   names, sheet positions or fixed offsets, and must reject rather than coerce invalid values.
4. Add tests that generate fixtures at run time, including at least one layout the contract must
   reject.
5. Add a `fetch_<source>` and `parse_<source>` task to `resources/refresh_performance.job.yml` and
   make `conform_silver` depend on the parse task. Add the source to `SILVER_FOR` in `tasks.py`.
6. If its values belong in an existing fact, add them to the conform or release SQL in
   `gold_build.py`; otherwise add a release store and Gold view in `gold.py`.
7. When the licence, attribution, release identity and contract are recorded, change the status
   to `enabled` (or `conditional` with a `licence_verification` route for each asset), and record
   the decision in [data-issues-and-decisions.md](data-issues-and-decisions.md).

## A metric mapping

Add or edit a metric in `metrics.yml`:

```yaml
  - metric_id: bathing_water_quality
    name: Bathing water quality
    theme: Environment
    description: Percentage of bathing waters meeting the standard.
    unit: percent
    price_base: not_applicable
    direction: higher_is_better
    service_scope: wastewater
    regulatory_period: AMP8
    comparability: comparable
    definition_source: PR24 common performance commitment; WCPR item <item reference>
    sources:
      - {source_id: OFWAT_WCPR, item_reference: <item reference>, unit: "% in no."}
```

Rules:

- Map only items that measure the same thing with the same definition. Similar labels are not
  enough; check the RAG 4 definition or the performance commitment definition.
- A `comparable` metric must state its unit, price base, direction, service scope and definition
  source. A `not_comparable` metric must state `comparability_reason`.
- A source rule matches only when every field it states matches. `unit` is the unit exactly as
  the source publishes it; a different unit makes the row non-comparable rather than converting it.
- For Stream rows whose attribute names a year, use `{financial_year}` in `attribute` so only the
  item's own year maps.

The quality gate `one_preferred_value` fails the release if a mapping produces two preferred
values for one metric, company and year.

## A company

Add a row to `companies.csv` with the Ofwat code, name, service type (`water_and_sewerage` or
`water_only`) and jurisdiction, plus any aliases publishers use. If a publisher reports the
company's regions separately, add the regional codes to `reporting_areas.csv` with
`reporting_area` values; only `whole_company` rows are ranked.

## A jurisdiction

Scotland and Northern Ireland need their own adapters: their returns use different tables,
definitions and price controls. Do not map them through the Ofwat parsers or metrics.

1. Register the sources (they are already listed as `WICS_RETURN` and `NI_AIR`).
2. Write jurisdiction-specific parsers and Silver tables.
3. Add `scotland` or `northern_ireland` to `SUPPORTED_JURISDICTIONS` in `registry.py`, and add the
   company with that jurisdiction to `companies.csv`.
4. Map only genuinely equivalent measures into `metrics.yml`, with `comparability_reason` for
   every measure that differs.
5. Update the Genie instructions and benchmarks, then redeploy the Genie bundle.

## A benchmark question

Add an entry to [eval/benchmark_questions.yaml](../eval/benchmark_questions.yaml) with a category,
the question, its critical failure classes, the expected behaviour and ground-truth SQL against
Gold (authoring names, `uk_water_performance_genie.water_performance_gold`). Questions about "we"
must read `deployment_context` or `is_configured_company`. Then:

```bash
uv run --no-project --with pyyaml python tools/sync_benchmarks.py
cd bundles/genie && databricks bundle deploy -t prod
```

This is the one change in this page that needs a Genie redeploy.
