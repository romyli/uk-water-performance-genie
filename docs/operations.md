# Operations

Commands run from `bundles/data` (or `bundles/genie` where stated) with your `BUNDLE_VAR_*`
values exported and `-t prod`. SQL uses the default schema names; replace `<catalog>`.

## The refresh job

`refresh_performance` runs these tasks, all on serverless compute:

| Task | What it does | If it fails |
|---|---|---|
| `setup` | Applies pending schema migrations, checks `company_code`, snapshots the registry and each source's gate result | Nothing else runs; fix the reported configuration |
| `discover_sources` | Lists public Stream APR items and records any not in `stream_apr_items.yml` (never ingested) | Other tasks continue |
| `fetch_<source>` | Retrieves each enabled or conditional asset into the raw Volume | That source keeps its last archived version |
| `inventory_archive` | Checksums retrievals and manual uploads, decides new/unchanged/changed, archives, verifies licences | Nothing is published |
| `parse_<source>` | Applies the source's contract to each new current version and writes Silver and rejects | That source keeps its previous Silver rows |
| `conform_silver` | Builds `silver.conformed_observation` from current, licence-verified versions | Nothing is published |
| `publish_gold` | Builds a candidate release and refreshes the Gold view definitions | Nothing is published |
| `quality_gates` | Runs the checks in `config/quality.yml` and the structural checks | The candidate is not published |
| `finalize` | Publishes the candidate if every blocking check passed; fails the run if any stage failed | The previous release stays live |

A run that publishes a new release but had a failed fetch or parse still ends as failed, so that
the failure notification goes out.

## Inspecting a run

```sql
-- Stages and sources for the latest run
SELECT stage, source_id, status, message, metrics
FROM <catalog>.water_performance_ops.pipeline_run
WHERE run_id = (SELECT max(run_id) FROM <catalog>.water_performance_ops.pipeline_run)
ORDER BY finished_at;

-- What was retrieved, skipped or blocked
SELECT source_id, asset_key, outcome, message
FROM <catalog>.water_performance_ops.retrieval_event
WHERE run_id = '<run id>' AND outcome <> 'unchanged_by_metadata';

-- Rejected records by reason
SELECT source_id, reason_code, count(*) AS records, any_value(record_locator) AS example
FROM <catalog>.water_performance_ops.reject
WHERE run_id = '<run id>'
GROUP BY source_id, reason_code ORDER BY records DESC;

-- Gate results for the latest candidate
SELECT check_id, severity, status, observed, expected, message
FROM <catalog>.water_performance_ops.quality_result
WHERE release_id = (SELECT max(release_id) FROM <catalog>.water_performance_ops.release)
ORDER BY status, severity;
```

Common reject reasons:

| Reason code | Meaning |
|---|---|
| `VALUE_MISSING`, `VALUE_PLACEHOLDER` | Empty cell or a placeholder such as `-` or `n/a`; not loaded, never zero |
| `VALUE_SPREADSHEET_ERROR` | `#N/A`, `#DIV/0!` and similar |
| `VALUE_NOT_NUMERIC` | Text where a number was expected, such as a credit rating or `Yes` |
| `CONFLICTING_VALUES` | Two rows with the same key and different values; both are held back |
| `UNKNOWN_COMPANY` | A company or region code missing from `companies.csv` or `reporting_areas.csv` |
| `COMPANY_CONFLICT`, `PERIOD_CONFLICT` | A record's company or year differs from its registered item |
| `CONTRACT_MISMATCH` | The asset's layout does not match its contract; the evidence column shows what was seen |
| `BOOTSTRAP_METADATA` | A manual upload's sidecar is missing a field or its checksum does not match |

## Manual bootstrap

Use this only when a fetch reports `blocked`, meaning the publisher returned a browser check or
login page instead of the file. Never work around the check.

1. Open the asset's landing page (in `sources.yml` and in the blocked message) in a browser and
   download the exact file.
2. From the repository root, check what would be uploaded:

   ```bash
   python3 tools/source_upload.py --target prod --source-id <source id> --asset-id <asset id> \
     --file <downloaded file> --landing-page <landing page URL> --asset-url <asset URL> \
     --release "<release name>" --published <YYYY-MM-DD> --licence-id OGL-UK-3.0 \
     --licence-evidence "<where the page states the licence>" --supplied-by "<your name or team>" --dry-run
   ```

   The dry run prints the Volume destination, the SHA-256 and the sidecar metadata.
3. Run the same command without `--dry-run`, then run `refresh_performance`. The inventory task
   recomputes the checksum, checks it against the sidecar and archives the file. It then goes
   through the same contract, gates and attribution as a downloaded file.

Files are never overwritten. A new release needs a new file name.

## Confirming a licence for a conditional source

PR24 Key Dataset 1 does not state its licence inside the workbook, so it stays quarantined
(`status = 'quarantined'` in `ops.asset_manifest`). If your organisation confirms from the
Ofwat page that the asset is released under the Open Government Licence v3.0:

1. In `sources.yml`, set the asset's `licence_verification: operator_confirmed` and record who
   confirmed it and where in the source's `licence.evidence`.
2. Deploy the data bundle and run `refresh_performance`. The inventory task verifies the held
   version, and the next release includes it.

## Schema migrations

Tables are created and upgraded by numbered migrations in `water_performance/migrations.py`, at
the start of every refresh. Each migration is recorded in `ops.schema_migration` and only runs
once. Migrations are additive and safe to repeat after a failure part-way through.

## Rolling back

```bash
databricks bundle run rollback_release -t prod --params release_id=<release id>
```

Find release IDs with `SELECT release_id, status, published_at FROM <catalog>.water_performance_ops.release ORDER BY created_at DESC`.
The rollback switches every Gold object in one commit. The next successful refresh publishes a
new release as normal.

## Scheduling and notifications

Runs are manual by default. To run weekly, set `refresh_schedule_status=UNPAUSED` and redeploy.
Set `notification_emails` to a JSON list to be emailed when any job fails.

## Validation

Run after every deployment or upgrade:

```bash
databricks bundle run validate_project -t prod
```

It runs `ruff`, the unit and repository tests, and integration tests that create and drop tables
in the test schema. Results are in `ops.test_result` and the `validation_reports` Volume.

## Genie benchmarks

The 24 questions in [eval/benchmark_questions.yaml](../eval/benchmark_questions.yaml) are
loaded into the Genie space as native benchmarks, each with ground-truth SQL. To run them, open the
space, go to **Benchmarks** and choose **Run all**, or from the command line:

```bash
databricks genie genie-create-eval-run <space id>
```

Acceptance gates for a release of the Genie definition:

- at least 70 per cent of questions assessed as good;
- no critical failure: wrong company scope, a ranking of non-comparable rows, a number not in Gold,
  or a missing or wrong citation;
- results marked as needing review count as not passed until a person reviews them;
- run the benchmarks again after every change to instructions, example SQL or attached objects,
  and keep the result for comparison;
- a subject-matter expert from your regulation team signs off the answers before wider use.

To change a benchmark, edit the YAML, then run
`uv run --no-project --with pyyaml python tools/sync_benchmarks.py` and redeploy the Genie bundle.
