#!/usr/bin/env python3
"""Run every benchmark's ground-truth SQL against your deployment's Gold schema.

    uv run --no-project --with pyyaml python tools/check_benchmark_sql.py \\
      --warehouse-id <warehouse ID> --gold <catalog>.<gold schema> [--profile <profile>] [--report out.json]

The SQL in eval/benchmark_questions.yaml uses the authoring names; this tool rewrites them with
the same code as the Genie bundle, runs each statement on the SQL warehouse through the Databricks
CLI, and reports the row count or the error. It changes nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bundles" / "genie"))

import genie_retarget  # noqa: E402

AUTHORING = "uk_water_performance_genie.water_performance_gold"


def run(sql: str, warehouse: str, profile: str | None) -> dict:
    body = json.dumps(
        {
            "statement": sql,
            "warehouse_id": warehouse,
            "wait_timeout": "50s",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        }
    )
    cmd = ["databricks", "api", "post", "/api/2.0/sql/statements", "--json", body] + (
        ["--profile", profile] if profile else []
    )
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return {"status": "ERROR", "error": (p.stderr or p.stdout).strip()[:500]}
    out = json.loads(p.stdout)
    state = (out.get("status") or {}).get("state")
    if state != "SUCCEEDED":
        return {"status": state, "error": ((out.get("status") or {}).get("error") or {}).get("message", "")[:500]}
    rows = (out.get("result") or {}).get("data_array") or []
    return {"status": "SUCCEEDED", "rows": len(rows), "first_row": rows[0] if rows else None}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--warehouse-id", required=True)
    p.add_argument("--gold", required=True, help="<catalog>.<gold schema> of the deployment")
    p.add_argument("--profile")
    p.add_argument("--report", type=Path)
    args = p.parse_args(argv)
    doc = yaml.safe_load((ROOT / "eval" / "benchmark_questions.yaml").read_text(encoding="utf-8"))
    rules = genie_retarget.parse_rules({AUTHORING: args.gold})
    results, failed = [], 0
    for q in doc["questions"]:
        sql = genie_retarget.retarget_text(q["sql"], rules)
        r = run(sql, args.warehouse_id, args.profile)
        failed += r["status"] != "SUCCEEDED"
        results.append({"id": q["id"], "category": q["category"], **r})
        print(f"{q['id']} {q['category']:<26} {r['status']:<10} rows={r.get('rows', '-')} {r.get('error', '')}")
    print(f"\n{len(results) - failed}/{len(results)} ground-truth statements ran successfully")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
