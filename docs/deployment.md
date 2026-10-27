# Deployment

The README has the complete zero-to-working path. This page adds the detail behind each step:
targets, variables, identities and what each bundle creates.

## Targets

Both bundles have the same two targets.

| Target | Use | Behaviour |
|---|---|---|
| `dev` | A developer's own deployment | Development mode: jobs are named `[dev <user>] ...`, schema names get a `dev_<user>_` prefix, schedules stay paused, files go under `<workspace_base>/bundles/<bundle>/dev` |
| `prod` | The customer deployment | Production mode: jobs run as `production_run_as`, groups get the permissions and grants listed below |

No target names a workspace host or CLI profile. Choose the workspace with your CLI profile
(`--profile` or `DATABRICKS_CONFIG_PROFILE`).

A development deployment uses the same commands with `-t dev`. The Genie bundle's dev target
adds the same `dev_<user>_` schema prefix so that it finds the dev Gold schema.

## Variables

Set each variable with `--var <name>=<value>` on every bundle command, or export
`BUNDLE_VAR_<name>` once. The names are the same in both bundles, in
[config.example.yml](../config.example.yml) and in the preflight output.

| Variable | Bundles | Required | Meaning |
|---|---|---|---|
| `catalog` | data, genie | yes | Existing catalog for the project |
| `workspace_base` | data, genie | yes | Workspace folder for bundle files and the Genie space |
| `warehouse_name` | data, genie | yes | SQL warehouse, by exact name; the Genie bundle looks up its ID |
| `company_code` | data | yes | Ofwat company code of the deploying company |
| `production_run_as` | data | prod | Application ID of the service principal |
| `analyst_group` | data, genie | prod | Group that reads Gold and uses Genie |
| `data_owner_group` | data | prod | Group that runs refreshes and uploads manual-bootstrap files |
| `notification_emails` | data | no | JSON list of addresses told when a job fails |
| `refresh_schedule_status` | data | no | `PAUSED` (default) or `UNPAUSED` |
| `refresh_schedule_cron` | data | no | Quartz cron, default `0 0 6 ? * MON` (Europe/London) |
| `landing_schema`, `ops_schema`, `silver_schema`, `gold_schema`, `test_schema` | data (genie: `landing_schema`, `gold_schema`) | no | Schema names; keep them identical in both bundles |
| `documents_volume` | data, genie | no | Name of the curated documents Volume |

## What the data bundle creates

| Object | Name (default) | Notes |
|---|---|---|
| Schemas | `water_performance_landing`, `_ops`, `_silver`, `_gold`, `_test` | Protected with `prevent_destroy` |
| Volumes | `landing.raw`, `landing.archive`, `landing.curated_documents`, `ops.validation_reports` | Managed; protected |
| Jobs | `uk-water-performance-refresh`, `uk-water-performance-validate`, `uk-water-performance-rollback` | Serverless only |

Tables and views are created by the jobs, not the bundle: numbered migrations at the start of
each refresh create or upgrade them (see [operations.md](operations.md#schema-migrations)).

Grants in the `prod` target:

| Securable | Principal | Privileges |
|---|---|---|
| Gold schema | analyst group, data owner group | `USE_SCHEMA`, `SELECT` |
| Ops schema | data owner group | `USE_SCHEMA`, `SELECT` |
| Landing schema | analyst group, data owner group | `USE_SCHEMA` |
| `raw` Volume | data owner group | `READ_VOLUME`, `WRITE_VOLUME` |
| `curated_documents` Volume | analyst group | `READ_VOLUME`; data owners also `READ_VOLUME` |
| Jobs | analyst group / data owner group | `CAN_VIEW` / `CAN_MANAGE_RUN` |
| Genie space | analyst group | `CAN_RUN` |

## What the Genie bundle creates

One Genie space, `Water company performance (England and Wales)`, in `workspace_base`, using the
warehouse named by `warehouse_name`. It attaches seven Gold objects and the curated documents
Volume:

- `deployment_context`, `vw_reported_performance`, `vw_commitment_vs_actual`, `vw_odi_position`,
  `vw_provenance`, `vw_source_coverage` (tables and views);
- `mv_peer_comparison` (Metric View).

The definition in `bundles/genie/src/water_performance.geniespace.json` is written against neutral
authoring names (`uk_water_performance_genie.water_performance_gold`). At validate and deploy
time, `genie_retarget.py` rewrites every name, quoted name and Volume path for your target from
the `genie_table_mapping` variable, sorts the data sources and fails if any authoring name is
left. The preflight checks the result again.

## Authenticating as the service principal

Deploying `prod` as the service principal makes it the owner of the schemas, Volumes, jobs, Gold
views and Genie space, so nothing depends on a person's account. Create an OAuth secret for the
service principal (workspace admin settings, or your identity provider) and add a profile:

```ini
[<profile>]
host          = https://<workspace host>
client_id     = <service principal application ID>
client_secret = <secret>
```

Then `export DATABRICKS_CONFIG_PROFILE=<profile>` before the README steps. If you deploy as a
person instead, the jobs still run as the service principal, but that person owns the deployed
objects and the service principal additionally needs `USE_SCHEMA`, `CREATE_TABLE`, `MODIFY`,
`SELECT`, `READ_VOLUME` and `WRITE_VOLUME` on the project schemas.

## Order of deployment

1. Data bundle: `validate --strict`, `deploy`.
2. `validate_project` job.
3. `refresh_performance` job. Gold views exist after the first run.
4. Genie bundle: `validate --strict`, `deploy`. Genie checks that every attached object exists,
   so it must come after the first successful refresh.

Replacing or adding public data later only needs step 3. Redeploy the Genie bundle only when its
instructions, example SQL, benchmarks or attached objects change.
