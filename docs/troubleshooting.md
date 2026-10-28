# Troubleshooting

## Preflight

| Message | Fix |
|---|---|
| `variable <name> is not set` | Export `BUNDLE_VAR_<name>` or pass `--var <name>=<value>`; see [config.example.yml](../config.example.yml) |
| `company_code 'X' is not in the company crosswalk. Accepted codes: ...` | Use one of the listed codes exactly. To add a company, see [adding-a-source.md](adding-a-source.md#a-company) |
| `expected exactly one warehouse named ...` | `warehouse_name` must match the warehouse name exactly, including spaces and case |
| `service principal with application ID ... was not found` | Use the application ID (a UUID), not the display name, and check the principal is added to the workspace |
| `... was not found in this workspace` (group) | Create the group or correct the name; account groups must be assigned to the workspace |
| `classic compute is not allowed` | A job was edited to use a cluster; every task must use the serverless `environment_key` |
| `stale authoring reference ... remains` | The Genie mapping did not cover a name; check `genie_table_mapping` and the schema variables match the data bundle |

## Deployment

| Symptom | Fix |
|---|---|
| `bundle validate` fails in `bundles/genie` with a Python or `databricks.bundles` error | Run `cd bundles/genie && uv sync`. The `databricks-bundles` version must not be newer than your CLI |
| Genie deploy fails because a table or view does not exist | Run `refresh_performance` successfully first; Gold views are created by the first refresh |
| `PERMISSION_DENIED` creating schemas | The deploying identity needs `USE CATALOG` and `CREATE SCHEMA` on the catalog |
| A job fails immediately with an environment error | Serverless compute for jobs must be enabled, and serverless must be able to install packages from the Python package index |

## Refresh

| Symptom | Where to look | Fix |
|---|---|---|
| A fetch task reports `blocked` | `ops.retrieval_event.message` | The publisher returned a browser check. Use the manual bootstrap command in the message ([operations.md](operations.md#manual-bootstrap)) |
| A fetch reports `failed` with HTTP 404 | `ops.retrieval_event` | The publisher moved the file. Find the new asset URL on its landing page, update `sources.yml`, and record the change in [data-issues-and-decisions.md](data-issues-and-decisions.md) |
| A Stream item reports `licence_rejected` | `ops.retrieval_event` | The item's licence is not CC BY 4.0. Leave it on hold; record the decision |
| `CONTRACT_MISMATCH` for an asset | `ops.reject.evidence` | The publisher changed the layout. Compare the evidence with the contract in the parser; extend the contract with the new labels and add a test |
| Many `CONFLICTING_VALUES` rejects | `ops.reject` | The source repeats a key with different values. They are held back, not guessed; check whether the key needs another field |
| `finalize` says the release was not published | `ops.quality_result` | Read the failing blocking check's `message`, `observed` and `expected`; the previous release is still live |
| A conditional source has no data | `vw_source_coverage`, `ops.asset_manifest.status_reason` | The asset has no licence statement inside it. Confirm the licence ([operations.md](operations.md#confirming-a-licence-for-a-conditional-source)) |
| `freshness_*` gate fails | `ops.quality_result` | The newest data is older than `quality.yml` allows. Register the publisher's newer release, or relax the threshold with a recorded decision |

## Genie answers

| Symptom | Fix |
|---|---|
| Questions or benchmarks fail with `INSUFFICIENT_PERMISSIONS ... does not have USE CATALOG` | Genie runs queries as the person asking. Grant them `USE CATALOG` on the catalog and `USE SCHEMA, SELECT` on the Gold schema; the `dev` target grants neither ([Sharing a development deployment](../README.md#sharing-a-development-deployment)). If the person already has access (check `databricks grants get-effective catalog <catalog> --principal <user>`) and chat questions work but benchmark runs still fail this way, grants will not fix it: the benchmark's ground-truth query is being refused. Report it to Databricks support with a failed statement ID from the benchmark result and a working chat statement ID |
| Genie answers for the wrong company | Check `SELECT * FROM <catalog>.water_performance_gold.deployment_context`. If it is wrong, fix `company_code`, redeploy the data bundle and refresh |
| Genie says data is not available when you expect it | Query `vw_source_coverage` for the source; check the year and company exist in `vw_reported_performance` |
| Genie ranks something it should not | Report it as a critical benchmark failure; check `is_comparable` and `comparability_reason` for the rows it used |
| An answer has no citation | Ask "Where does that number come from?"; Genie should query `vw_provenance` |

## Getting help

When raising an issue, include the job run ID, the failing task, and the relevant rows from
`ops.pipeline_run`, `ops.reject` or `ops.quality_result`. Do not attach downloaded source files;
give their URLs and checksums instead.
