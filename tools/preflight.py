#!/usr/bin/env python3
"""Read-only deployment preflight for both bundles.

Resolves the data and Genie bundles for one target, prints where everything will go, and fails
on anything that would make the deployment unsafe or misleading:

* unresolved variables or a missing required value;
* a schema, Volume or workspace path outside the configured catalog and workspace_base;
* classic compute (clusters, pools, node types) in any job;
* a warehouse that cannot be found by name;
* a company_code that is not in the company crosswalk;
* invalid grant privileges, or missing principals for the prod target;
* Genie data sources outside the Gold schema, or authoring names left after retargeting.

It makes no changes. Run it from the repository root:

    python3 tools/preflight.py --target prod --profile <profile> --var catalog=<catalog> ...

Variables can also come from BUNDLE_VAR_<name> environment variables, exactly as for
`databricks bundle`. Uses only the Python standard library and the Databricks CLI.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "bundles" / "data"
GENIE = ROOT / "bundles" / "genie"
COMPANIES = DATA / "src" / "water_performance" / "config" / "companies.csv"

CLASSIC_KEYS = {
    "new_cluster",
    "existing_cluster_id",
    "job_clusters",
    "instance_pool_id",
    "node_type_id",
    "driver_node_type_id",
    "cluster_id",
    "job_cluster_key",
    "policy_id",
}
VALID_PRIVILEGES = {
    "schemas": {
        "ALL_PRIVILEGES",
        "APPLY_TAG",
        "CREATE_FUNCTION",
        "CREATE_MATERIALIZED_VIEW",
        "CREATE_MODEL",
        "CREATE_TABLE",
        "CREATE_VOLUME",
        "EXECUTE",
        "MANAGE",
        "MODIFY",
        "READ_VOLUME",
        "REFRESH",
        "SELECT",
        "USE_SCHEMA",
        "WRITE_VOLUME",
    },
    "volumes": {"ALL_PRIVILEGES", "APPLY_TAG", "MANAGE", "READ_VOLUME", "WRITE_VOLUME"},
}
REQUIRED_VARS = ("catalog", "workspace_base", "warehouse_name", "company_code")
PROD_VARS = ("production_run_as", "analyst_group", "data_owner_group")
DEPLOY_TIME_REFS = re.compile(r"\$\{resources\.(schemas|volumes)\.[a-z_]+\.name\}")


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  FAIL  {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  WARN  {msg}")

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")


def cli(args: list[str], profile: str | None, cwd: Path | None = None) -> tuple[int, str, str]:
    cmd = ["databricks", *args] + (["--profile", profile] if profile else [])
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def bundle_variables(bundle_dir: Path) -> set[str]:
    text = (bundle_dir / "databricks.yml").read_text(encoding="utf-8")
    block = text.split("\nvariables:\n", 1)[1].split("\ntargets:", 1)[0]
    return set(re.findall(r"^  ([a-z_]+):", block, re.MULTILINE))


def validate(bundle_dir: Path, target: str, profile: str | None, values: dict, report: Report) -> dict | None:
    known = bundle_variables(bundle_dir)
    var_args = [a for k, v in values.items() if k in known for a in ("--var", f"{k}={v}")]
    code, out, err = cli(["bundle", "validate", "--strict", "-t", target, "-o", "json", *var_args], profile, bundle_dir)
    if code != 0:
        report.fail(f"bundle validate failed in {bundle_dir.relative_to(ROOT)}: {(err or out).strip()[:1500]}")
        return None
    report.ok(f"strict validation passed for {bundle_dir.relative_to(ROOT)} ({target})")
    return json.loads(out)


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, node


def unresolved(doc: dict) -> list[str]:
    bad = []
    for path, value in walk(doc.get("resources") or {}):
        if isinstance(value, str) and "${" in DEPLOY_TIME_REFS.sub("", value):
            bad.append(f"{path} = {value[:120]}")
    return bad


def classic_compute(doc: dict) -> list[str]:
    found = []
    for name, job in ((doc.get("resources") or {}).get("jobs") or {}).items():
        for path, _ in walk(job):
            leaf = re.sub(r"\[\d+\]", "", path).split(".")
            if CLASSIC_KEYS & set(leaf):
                found.append(f"jobs.{name}: {path}")
        for t in job.get("tasks") or []:
            if not t.get("environment_key") and not t.get("sql_task") and not t.get("run_job_task"):
                found.append(f"jobs.{name}.{t.get('task_key')}: task has no serverless environment_key")
    return found


def read_vars(args) -> dict[str, str]:
    values = {k[len("BUNDLE_VAR_") :]: v for k, v in os.environ.items() if k.startswith("BUNDLE_VAR_")}
    for item in args.var or []:
        if "=" not in item:
            raise SystemExit(f"--var must look like name=value, got {item!r}")
        k, v = item.split("=", 1)
        values[k] = v
    return values


def load_companies() -> dict[str, dict]:
    with COMPANIES.open(encoding="utf-8", newline="") as f:
        return {r["company_code"]: r for r in csv.DictReader(f)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", required=True, help="bundle target, for example dev or prod")
    p.add_argument("--profile", help="Databricks CLI profile (default: the CLI's default authentication)")
    p.add_argument("--var", action="append", help="bundle variable name=value (repeatable)")
    p.add_argument("--skip-genie", action="store_true", help="check the data bundle only (before Gold exists)")
    args = p.parse_args(argv)
    values = read_vars(args)
    report = Report()

    print(f"\nPreflight for target '{args.target}' (read-only)\n")
    print("Configuration")
    missing = [k for k in REQUIRED_VARS if not values.get(k)]
    if args.target == "prod":
        missing += [k for k in PROD_VARS if not values.get(k)]
    for k in missing:
        report.fail(f"variable {k} is not set (use --var {k}=<value> or BUNDLE_VAR_{k})")

    companies = load_companies()
    code = values.get("company_code", "")
    if code and code not in companies:
        report.fail(
            f"company_code {code!r} is not in the company crosswalk. Accepted codes: {', '.join(sorted(companies))}"
        )
    elif code:
        c = companies[code]
        report.ok(f"company {code}: {c['company_name']}, {c['service_type']}, {c['jurisdiction']}")

    code_, out, err = cli(["current-user", "me", "-o", "json"], args.profile)
    if code_ != 0:
        report.fail(f"cannot authenticate: {(err or out).strip()[:300]}")
        return finish(report)
    me = json.loads(out)
    code_, out, _ = cli(["auth", "env", "-o", "json"], args.profile)
    host = (json.loads(out).get("env") or {}).get("DATABRICKS_HOST") if code_ == 0 else None
    print(f"  ok    authenticated as {me.get('userName')} on {host or '(host from CLI configuration)'}")

    print("\nData bundle")
    data = validate(DATA, args.target, args.profile, values, report)
    if data:
        check_data(data, values, args.target, report)

    print("\nWarehouse")
    check_warehouse(values.get("warehouse_name"), args.profile, report)

    if args.target == "prod":
        print("\nPrincipals")
        check_principals(values, args.profile, report)

    if not args.skip_genie:
        print("\nGenie bundle")
        genie = validate(GENIE, args.target, args.profile, values, report)
        if genie and data:
            check_genie(genie, data, report)
    return finish(report)


def check_data(doc: dict, values: dict, target: str, report: Report) -> None:
    res = doc.get("resources") or {}
    catalog = values.get("catalog")
    root = (doc.get("workspace") or {}).get("root_path", "")
    base = values.get("workspace_base", "")
    print(f"  root  {root}")
    if not base or not root.startswith(base.rstrip("/") + "/"):
        report.fail(f"workspace root {root!r} is outside workspace_base {base!r}")
    schemas = res.get("schemas") or {}
    names = {k: s.get("name") for k, s in schemas.items()}
    for key, s in schemas.items():
        print(f"  schema {s.get('catalog_name')}.{s.get('name')}")
        if s.get("catalog_name") != catalog:
            report.fail(f"schema {key} is in catalog {s.get('catalog_name')!r}, not {catalog!r}")
        check_grants("schemas", key, s.get("grants") or [], report)
    for key, v in (res.get("volumes") or {}).items():
        schema = v.get("schema_name", "")
        m = re.fullmatch(r"\$\{resources\.schemas\.([a-z_]+)\.name\}", schema)
        schema = names.get(m.group(1), schema) if m else schema
        print(f"  volume /Volumes/{v.get('catalog_name')}/{schema}/{v.get('name')}")
        if v.get("catalog_name") != catalog:
            report.fail(f"volume {key} is in catalog {v.get('catalog_name')!r}, not {catalog!r}")
        check_grants("volumes", key, v.get("grants") or [], report)
    for key, job in (res.get("jobs") or {}).items():
        print(
            f"  job   {job.get('name')} ({len(job.get('tasks') or [])} tasks, run as "
            f"{(doc.get('run_as') or {}).get('service_principal_name') or 'deploying identity'})"
        )
        for t in job.get("tasks") or []:
            f = (t.get("spark_python_task") or {}).get("python_file", "")
            if f and not f.startswith(root):
                report.fail(f"jobs.{key}.{t.get('task_key')}: {f} is outside the bundle root")
    bad = unresolved(doc)
    for b in bad:
        report.fail(f"unresolved value {b}")
    classic = classic_compute(doc)
    for c in classic:
        report.fail(f"classic compute is not allowed: {c}")
    if not bad and not classic:
        report.ok("every job task runs on serverless compute; no unresolved values")
    if target == "prod" and not (doc.get("run_as") or {}).get("service_principal_name"):
        report.fail("prod must run as a service principal (production_run_as)")


def check_grants(kind: str, key: str, grants: list[dict], report: Report) -> None:
    for g in grants:
        principal = g.get("principal")
        if not principal:
            report.fail(f"{kind}.{key}: a grant has an empty principal")
        bad = sorted(set(g.get("privileges") or []) - VALID_PRIVILEGES[kind])
        if bad:
            report.fail(f"{kind}.{key}: invalid privileges {bad} for {kind}")
        else:
            print(f"  grant {kind}.{key}: {principal} {g.get('privileges')}")


def check_warehouse(name: str | None, profile: str | None, report: Report) -> None:
    if not name:
        return
    code, out, err = cli(["warehouses", "list", "-o", "json"], profile)
    if code != 0:
        report.fail(f"cannot list warehouses: {(err or out).strip()[:300]}")
        return
    matches = [w for w in json.loads(out) if w.get("name") == name]
    if len(matches) != 1:
        report.fail(f"expected exactly one warehouse named {name!r}, found {len(matches)}")
        return
    w = matches[0]
    report.ok(
        f"warehouse {name!r} -> id {w.get('id')}, state {w.get('state')}, "
        f"serverless {w.get('enable_serverless_compute')}"
    )


def check_principals(values: dict, profile: str | None, report: Report) -> None:
    sp = values.get("production_run_as")
    if sp:
        code, out, _ = cli(["service-principals", "list", "--filter", f"applicationId eq {sp}", "-o", "json"], profile)
        found = json.loads(out) if code == 0 and out.strip() else []
        if not found:
            report.fail(f"service principal with application ID {sp!r} was not found in this workspace")
        else:
            report.ok(f"run as service principal {found[0].get('displayName')} ({sp})")
    for key in ("analyst_group", "data_owner_group"):
        g = values.get(key)
        if not g:
            continue
        code, out, _ = cli(["groups", "list", "--filter", f'displayName eq "{g}"', "-o", "json"], profile)
        found = json.loads(out) if code == 0 and out.strip() else []
        if not found:
            report.fail(f"{key} {g!r} was not found in this workspace")
        else:
            report.ok(f"{key}: {g}")


def check_genie(genie: dict, data: dict, report: Report) -> None:
    schemas = {k: s.get("name") for k, s in ((data.get("resources") or {}).get("schemas") or {}).items()}
    catalog = next(iter(((data.get("resources") or {}).get("schemas") or {}).values()), {}).get("catalog_name")
    gold = f"{catalog}.{schemas.get('gold')}"
    docs_prefix = f"/Volumes/{catalog}/{schemas.get('landing')}/"
    spaces = (genie.get("resources") or {}).get("genie_spaces") or {}
    if not spaces:
        report.fail("the Genie bundle defines no genie_spaces resource")
    authoring = authoring_prefixes()
    for key, space in spaces.items():
        raw = space.get("serialized_space")
        if not raw:
            report.fail(f"genie_spaces.{key}: serialized_space was not produced (did the retarget mutator run?)")
            continue
        body = json.loads(raw) if isinstance(raw, str) else raw
        print(
            f"  space {space.get('title')!r} -> warehouse {space.get('warehouse_id')}, "
            f"parent {space.get('parent_path')}"
        )
        ds = body.get("data_sources") or {}
        objects = [i["identifier"] for k in ("tables", "metric_views") for i in ds.get(k) or []]
        for ident in objects:
            if not ident.lower().startswith(gold.lower() + "."):
                report.fail(f"genie_spaces.{key}: data source {ident} is not in the Gold schema {gold}")
        for v in ds.get("volumes") or []:
            if not v.get("path", "").startswith(docs_prefix):
                report.fail(f"genie_spaces.{key}: Volume {v.get('path')} is not the curated documents Volume")
        text = json.dumps(body)
        for prefix in authoring:
            if prefix.lower() != gold.lower() and re.search(rf"(?<![\w.`]){re.escape(prefix)}\.", text, re.IGNORECASE):
                report.fail(f"genie_spaces.{key}: stale authoring reference {prefix} remains")
        if "${" in text or "{{" in text:
            report.fail(f"genie_spaces.{key}: placeholders remain in the serialized space")
        if space.get("file_path"):
            report.fail(f"genie_spaces.{key}: file_path is still set after retargeting")
        report.ok(f"genie_spaces.{key}: {len(objects)} Gold objects, all under {gold}")


def authoring_prefixes() -> list[str]:
    text = (GENIE / "databricks.yml").read_text(encoding="utf-8")
    block = text.split("genie_table_mapping:", 1)[1].split("\ntargets:", 1)[0]
    return re.findall(r"^\s{6}([A-Za-z0-9_]+\.[A-Za-z0-9_]+):", block, re.MULTILINE)


def finish(report: Report) -> int:
    print()
    if report.failures:
        print(f"Preflight FAILED: {len(report.failures)} problem(s). Nothing was changed.")
        return 1
    print(f"Preflight passed with {len(report.warnings)} warning(s). Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
