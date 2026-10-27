# UK Water Performance Genie

Ask plain-English questions about an England and Wales water company's regulatory performance,
and how it compares with other companies, using public regulator and company data only.

## What this project does

The project deploys two things into your Databricks workspace:

- a **data pipeline** that downloads public data from Ofwat and the Stream Water Data Portal,
  keeps an immutable, checksummed copy of every version, checks each source's licence, and
  publishes a governed set of tables;
- a **Genie space** where analysts ask questions such as "Which performance commitments did we
  miss in 2024-25?" or "Where do we rank on supply interruptions?".

It is for water company analysts, regulation and strategy teams, and the Databricks operators who
support them. You choose your company with one setting, `company_code`. Questions about "we",
"us" and "our" then mean your company, while every other company's data stays available for
comparison.

**Scope:** England and Wales companies regulated by Ofwat. Scotland (Scottish Water) and
Northern Ireland (NI Water) use different regulatory returns and are not covered.

## What you can ask

| You can ask | For example |
|---|---|
| Your performance commitments | "Which of our PR19 performance commitments did we meet in 2023-24?" |
| Targets against actuals | "What was our supply interruptions target and actual for each year of 2020-25?" |
| Rewards and penalties | "What were our net ODI payments by year, after aggregate sharing?" |
| Trends | "How has our per capita consumption changed since 2017-18?" |
| Peer comparison | "Where do we rank on internal sewer flooding in 2023-24?" |
| Another company | "Compare Severn Trent Water and United Utilities on mains repairs." |
| Finance (where published on Stream) | "Show Yorkshire Water's statutory revenue and dividends over time." |
| Future targets | "What are our PR24 leakage commitment levels for 2025-30?" (when PR24 data is loaded) |
| Where a number came from | "Where does our 2023-24 C-MeX score come from?" |

Genie will say a figure is **not available in the public sources held** rather than guess. It
cannot currently answer questions about:

- Environment Agency environmental performance ratings or storm overflow spills;
- Drinking Water Inspectorate sample results;
- CCW complaint figures;
- Scottish Water or NI Water;
- live operational data, internal company data, or anything not in a published source.

It will not rank companies on a measure that is not like-for-like (for example total leakage in
Ml/d, which depends on network size); it explains why and offers a comparable measure.

## How it works

```text
 Ofwat assets        Stream ArcGIS API        Operator upload (only if a publisher blocks automation)
      |                     |                        |
      v                     v                        v
 +---------------------------------------------------------------+
 | raw Volume: each retrieval as received, with fetch metadata    |
 +---------------------------------------------------------------+
      |  inventory: SHA-256, licence check, version, archive
      v
 +---------------------------------------------------------------+
 | archive Volume + ops.asset_manifest (immutable, by checksum)   |
 +---------------------------------------------------------------+
      |  one parser per source, contract checks, rejects recorded
      v
 +---------------------------------------------------------------+
 | Silver: source-specific tables, conformed observations         |
 +---------------------------------------------------------------+
      |  metric crosswalk, comparability rules, quality gates
      v
 +---------------------------------------------------------------+
 | Gold: views over one published release (switched atomically)  |
 +---------------------------------------------------------------+
      |
      v
   Genie space (reads Gold only)
```

- **Data bundle** (`bundles/data`): creates the schemas and Volumes, the `refresh_performance`
  job that runs the pipeline, the `validate_project` job that tests the project, and the
  `rollback_release` job. Every task runs on Databricks serverless compute.
- **Raw and archive Volumes**: the raw Volume holds each retrieval as received; the archive
  Volume holds one immutable copy of every distinct version, addressed by its SHA-256.
- **Operational tables** (`water_performance_ops`): the manifest of every asset version, run
  logs, rejected records, quality results and the list of Gold releases.
- **Silver** (`water_performance_silver`): one table per source, plus the release stores that
  hold each Gold release.
- **Gold** (`water_performance_gold`): views and a Metric View over the single published release.
  A new release becomes visible only after every blocking quality check passes, and all Gold
  objects switch to it in one commit.
- **Genie bundle** (`bundles/genie`): the Genie space, its instructions, example SQL and
  benchmark questions. It reads Gold only.

## Data sources

| Source | Status | What it provides | Licence |
|---|---|---|---|
| `OFWAT_WCPR` | Enabled | Ofwat Water Company Performance Report open data: comparable performance measures for every company, mostly from 2019-20 (some from 2017-18) to 2024-25 | Open Government Licence v3.0 |
| `STREAM_APR` | Enabled | Each participating company's standardised Annual Performance Report tables, 2023-24 to 2025-26, including statutory finance | CC BY 4.0, checked per item |
| `OFWAT_PR19_ODI` | Conditional | PR19 (2020-25) commitment levels, actuals, whether each was met, and ODI payments | Open Government Licence v3.0, verified in each workbook |
| `OFWAT_PR24_OUTCOMES` | Conditional | PR24 (2025-30) performance commitment levels | Open Government Licence v3.0, once verified |
| `RAG_DEFINITIONS` | Conditional | Ofwat's RAG 4 definitions of the APR tables, as a citable document | Open Government Licence v3.0, verified in the document |
| `COMPANY_APR`, `EA_EPA`, `EA_EDM`, `DWI_COMPANY`, `CCW_COMPLAINTS`, `COMPANY_BOUNDARIES` | On hold | Not loaded until bulk access, schema and reuse terms are confirmed | See the register |

A **conditional** source is loaded only when a licence statement is found inside the asset or an
operator records it (see [Routine operations](#routine-operations)). The full register, with exact
URLs, coverage notes and the reasons for each hold, is in [sources.md](sources.md). How
sources are accepted and versioned is in [docs/source-audit.md](docs/source-audit.md).

Known coverage gaps: Thames Water does not publish its APR tables on Stream, so Thames finance
lines are not available; Southern Water's 2024-25 Stream item has no open licence.

## Before you start

You need:

| Requirement | Detail |
|---|---|
| Databricks workspace | Unity Catalog enabled, serverless compute for jobs enabled, Genie (AI/BI) enabled |
| Catalog | An existing Unity Catalog catalog for this project |
| SQL warehouse | A serverless or pro SQL warehouse, referred to by its name |
| Service principal | Runs the production jobs and owns the deployed objects |
| Two groups | Analysts (read Gold, use Genie) and data owners (run refreshes, read operational tables) |
| Databricks CLI | Version 1.21 or later (`databricks --version`) |
| Python | 3.10 or later on the machine you deploy from, for the preflight and upload tools |
| uv | To create the small environment the Genie bundle uses at deploy time ([install uv](https://docs.astral.sh/uv/)) |
| Network | Serverless compute must reach `www.ofwat.gov.uk`, `www.arcgis.com`, `*.arcgis.com`, `www.streamwaterdata.co.uk` and the Python package index |

Permissions an administrator must grant once (step 2 below):

- the service principal: `USE CATALOG` and `CREATE SCHEMA` on the catalog, `CAN USE` on the
  warehouse, and `CAN MANAGE` on the workspace folder you deploy to;
- the analyst and data owner groups: `USE CATALOG` on the catalog and `CAN USE` on the warehouse.

The bundles grant everything else: `SELECT` on Gold to analysts, operational tables and upload
access to data owners, and `CAN RUN` on the Genie space to analysts.

Expected time: about 20 minutes of setup, then 45 to 60 minutes for the first refresh, most of it
downloading and parsing about 2.4 million Stream records. Later refreshes skip unchanged sources
and take about 10 minutes.

## Deploy to a new Databricks workspace

Run every command from the repository root unless a step says otherwise. Replace each
`<placeholder>`, including the angle brackets.

**1. Get the code and sign in.**

```bash
git clone <repository URL> uk-water-performance-genie
cd uk-water-performance-genie
databricks auth login --host https://<workspace host> --profile <profile>
export DATABRICKS_CONFIG_PROFILE=<profile>
databricks current-user me
```

Success: the last command prints your user. Production deployments should use a profile that
authenticates as the service principal (OAuth machine-to-machine); see
[docs/deployment.md](docs/deployment.md#authenticating-as-the-service-principal).

**2. Grant the one-time permissions.** An administrator runs this in a SQL editor:

```sql
GRANT USE CATALOG, CREATE SCHEMA ON CATALOG <catalog> TO `<service principal application ID>`;
GRANT USE CATALOG ON CATALOG <catalog> TO `<analyst group>`;
GRANT USE CATALOG ON CATALOG <catalog> TO `<data owner group>`;
```

Then give the service principal and both groups **Can use** on the SQL warehouse (warehouse
**Permissions** in the UI), and create the workspace folder and give the service principal
**Can manage** on it:

```bash
databricks workspace mkdirs <workspace folder>
```

Success: the grants run without errors and the folder exists.

**3. Set your configuration.** Every value is a bundle variable; exporting it as
`BUNDLE_VAR_<name>` applies it to every command below.
[config.example.yml](config.example.yml) explains each one.

```bash
export BUNDLE_VAR_catalog=<catalog>
export BUNDLE_VAR_workspace_base=<workspace folder>
export BUNDLE_VAR_warehouse_name="<SQL warehouse name>"
export BUNDLE_VAR_company_code=<company code>
export BUNDLE_VAR_production_run_as=<service principal application ID>
export BUNDLE_VAR_analyst_group="<analyst group>"
export BUNDLE_VAR_data_owner_group="<data owner group>"
```

`company_code` is your Ofwat company code, for example `SVE` for Severn Trent Water. The
accepted codes are listed in [config.example.yml](config.example.yml).

**4. Prepare the Genie bundle's environment.**

```bash
cd bundles/genie && uv sync && cd ../..
```

Success: `uv` reports that `databricks-bundles` is installed. This environment only rewrites
table names in the Genie definition during deployment; no data is processed on your machine.

**5. Run the preflight.**

```bash
python3 tools/preflight.py --target prod
```

Success: `Preflight passed with 0 warning(s). Nothing was changed.` The preflight prints the
workspace, catalog, schemas, Volumes, warehouse and its ID, company, principals, jobs and the Genie
space's data sources. It fails with the reason if any value is missing, a company code is unknown,
a principal or warehouse does not exist, a job would use anything other than serverless compute,
or the Genie definition would point outside your Gold schema.

**6. Validate and deploy the data bundle.**

```bash
cd bundles/data
databricks bundle validate --strict -t prod
databricks bundle deploy -t prod
```

Success: `Validation OK!`, then a `Resources:` summary with no error. The first deploy creates
5 schemas, 4 Volumes and 3 jobs.

**7. Run the project tests on your workspace.** Still in `bundles/data`:

```bash
databricks bundle run validate_project -t prod
```

Success: the output ends with `integration: {'passed': 5, 'failed': 0, 'skipped': 0}` after the
lint and unit lines also report no failures. The tests ran on serverless compute; reports are in
the `validation_reports` Volume.

**8. Bootstrap sources a publisher blocks (usually nothing to do).** The first refresh downloads
every enabled source itself. If a source reports `blocked` (step 9), its message prints the exact
manual-bootstrap command: download the file in a browser from the publisher's page and upload it
with `tools/source_upload.py`. See [docs/operations.md](docs/operations.md#manual-bootstrap).

**9. Run the first refresh.** Still in `bundles/data`:

```bash
databricks bundle run refresh_performance -t prod
```

Success, after 45 to 60 minutes: the output ends with `finalize : {'published': '<release ID>', ...}`. If the run fails,
the message names the stage, and the previous release (none, on a first run) stays published. See
[docs/troubleshooting.md](docs/troubleshooting.md).

**10. Check the quality gates.** In a SQL editor:

```sql
SELECT check_id, severity, status, observed, expected
FROM <catalog>.water_performance_ops.quality_result
WHERE release_id = (SELECT release_id FROM <catalog>.water_performance_ops.published_release)
ORDER BY severity, check_id;
```

Success: every `blocking` row has status `pass`. A `warning` row with status `fail` is expected
when a conditional source is not yet licence-verified.

**11. Validate and deploy the Genie bundle.** From the repository root (`cd ../..` from `bundles/data`):

```bash
cd bundles/genie
databricks bundle validate --strict -t prod
databricks bundle deploy -t prod
databricks bundle summary -t prod
```

Success: `Validation OK!`, the deploy creates `genie_spaces.water_performance`, and
`bundle summary` prints the Genie space URL.

**12. Ask the smoke questions.** Open the space URL and ask:

1. "Which company is this space configured for?" It should name your company.
2. "Which performance commitments did we miss in 2024-25?" It should say it is interpreting
   "we" as your company.
3. "Rank companies on water supply interruptions in 2023-24." It should list comparable companies.
4. "What is our EPA star rating?" It should say the data is not available in the public sources held.
5. "Where does our 2023-24 C-MeX score come from?" It should give the release, record locator and
   checksum.

To measure answer quality, run the benchmark questions as described in
[docs/operations.md](docs/operations.md#genie-benchmarks).

More detail for each step, including the development target, is in
[docs/deployment.md](docs/deployment.md).

## Choosing your company

`company_code` is the only company setting. The refresh writes a one-row
`deployment_context` table from the governed company list
(`bundles/data/src/water_performance/config/companies.csv`), which supplies the name, service type
and jurisdiction. An unknown code stops the refresh with the list of accepted codes; a near
match such as `Thames` is rejected, not guessed.

| Setting | "How are we doing on supply interruptions?" | "Rank us on internal sewer flooding" |
|---|---|---|
| `company_code=SVE` | Severn Trent Water's figures, introduced with "'We', 'us', 'our' and 'my company' mean Severn Trent Water (SVE), a water and sewerage company in England and Wales." | Severn Trent's rank among water and sewerage companies |
| `company_code=AFW` | Affinity Water's figures | No rank: Affinity Water is a water-only company, so wastewater measures such as internal sewer flooding are not compared for it |

Naming a company always overrides the setting: "Compare Severn Trent and United Utilities" uses
those two companies whatever `company_code` is. To change company, set the new code, redeploy the
data bundle and run `refresh_performance`; the Genie space does not need redeploying.

## Routine operations

Run these from `bundles/data` with the same `BUNDLE_VAR_*` values exported.

- **Refresh sources:** `databricks bundle run refresh_performance -t prod`. Unchanged sources are
  detected by API metadata or checksum and skipped. To refresh on a schedule, set
  `BUNDLE_VAR_refresh_schedule_status=UNPAUSED` (weekly on Monday at 06:00 UK time by default, set
  by `refresh_schedule_cron`) and redeploy.
- **Inspect failures:** `ops.pipeline_run` has one row per stage and source; `ops.reject` holds
  every rejected record with its locator and reason; `ops.quality_result` holds the gate results.
  Queries are in [docs/operations.md](docs/operations.md#inspecting-a-run).
- **Add or change a metric mapping, company, source release or benchmark:** follow
  [docs/adding-a-source.md](docs/adding-a-source.md). Changing data or mappings needs a data
  bundle deploy and a refresh, not a Genie redeploy. Changing Genie instructions or benchmarks
  needs a Genie bundle deploy.
- **Roll back a release:** `databricks bundle run rollback_release -t prod --params release_id=<release ID>`,
  using an ID from `ops.release`.
- **Upgrade:** pull the new version, run the preflight, deploy the data bundle, run
  `validate_project`, then `refresh_performance`, then deploy the Genie bundle. Schema changes are
  applied by numbered migrations at the start of the refresh and recorded in
  `ops.schema_migration`.

## Tracing a number to its source

Every Gold value carries its source, release, file or API record locator and checksum.
`gold.vw_provenance` adds the publisher URLs, retrieval time and licence:

```sql
SELECT original_label, value, unit, source_id, release_label, record_locator, sha256,
       asset_url, landing_page, retrieved_at, licence_id, archive_path
FROM <catalog>.water_performance_gold.vw_provenance
WHERE company_code = '<company code>'
  AND metric_id = 'per_capita_consumption'
  AND financial_year = '2023-24';
```

How to read the result:

1. `record_locator` says where the value sits: for Ofwat files, `file=...;sheet=...;row=...`; for
   Stream, `api_item=<item ID>;ObjectId=<n>`.
2. `sha256` identifies the exact archived bytes (files) or the canonical record set (API
   snapshots). The archived copy is at `archive_path`.
3. `asset_url` is the public asset or API service; `landing_page` is the publisher's page for it;
   `licence_id` is the licence the source gate accepted.
4. Silver keeps the original label, unit and raw value (`silver.ofwat_wcpr_record`,
   `silver.stream_apr_record`), and `ops.asset_manifest` keeps the HTTP or API metadata of the
   retrieval.

[docs/source-audit.md](docs/source-audit.md) walks through a full trace and the attribution notice.

## Security, costs and limitations

### Security model

- Genie and analysts read only the Gold schema. Gold views are owned by the deploying service
  principal, so analysts need no access to raw, archive, operational or Silver data.
- Production jobs run as the service principal. Analysts get `CAN VIEW` on jobs and `CAN RUN` on
  the Genie space; data owners get `CAN MANAGE RUN` on jobs, write access to the raw Volume for
  manual uploads, and read access to operational tables.
- No credentials are stored: all sources are public and need no keys. The repository contains no
  workspace hosts, warehouse IDs or user names.

### Costs and compute

Jobs use serverless compute and are billed per run. A refresh with no source changes uses a few
minutes of compute; the first refresh is the most expensive. Genie questions run on your SQL
warehouse. Storage is small: each Gold release is kept for the last five published releases.

### Known limitations

- England and Wales only.
- WCPR open data in the current releases covers 2019-20 to 2024-25 for most measures (2017-18 for a
  few). Stream items cover 2023-24 to 2025-26 and not every company publishes every table there
  (many omit the table 3 performance tables).
- PR24 levels need a verified Open Government Licence statement; the current Ofwat workbook does
  not include one, so an operator must confirm the licence before the levels load.
- PR19 ODI payments are in 2017-18 prices; finance lines from Stream are in nominal prices. Genie
  states the price base and does not convert between them.
- Numbers are as published. Restated values are flagged (`restated`, `previous_release_value`)
  rather than overwritten.

### Troubleshooting

The common problems (a blocked publisher, an unknown company code, a failed quality gate, a
Genie deployment that cannot find a table) and their fixes are in
[docs/troubleshooting.md](docs/troubleshooting.md).

### Safe cleanup

Schemas and Volumes are protected from accidental deletion (`prevent_destroy`). To remove the Genie
space and the jobs but keep the data, unbind the protected objects first so that `destroy` only
removes what is still bound:

```bash
cd bundles/genie && databricks bundle destroy -t prod
cd ../data
for key in landing ops silver gold test raw archive curated_documents validation_reports; do
  databricks bundle deployment unbind "$key" -t prod
done
databricks bundle destroy -t prod
```

The schemas, Volumes and tables stay in the catalog. To delete the data as well, an administrator
drops the schemas explicitly after checking their names (`DROP SCHEMA <catalog>.<schema> CASCADE`).
Nothing outside the configured catalog and workspace folder is touched.

## Licence

Code: Apache License 2.0 ([LICENSE](LICENSE)). Data loaded by the pipeline keeps its publisher's
licence; the attribution notice is generated in `gold.source_attribution`. Contributions are
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
