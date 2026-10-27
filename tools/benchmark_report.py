#!/usr/bin/env python3
"""Summarise a native Genie benchmark run against the acceptance gates.

    databricks genie genie-create-eval-run <space id>          # start a run; note eval_run_id
    uv run --no-project --with pyyaml python tools/benchmark_report.py \\
      --space-id <space id> --eval-run-id <eval run id> [--profile <profile>] [--report out.json]

Gates (eval/benchmark_questions.yaml): the share of questions assessed GOOD must reach
min_pass_rate; NEEDS_REVIEW counts as not passed until a person reviews it; and there must be no
critical failure. The tool flags likely critical failures it can detect from the generated SQL:
a "we/our" question that does not read the configured company, and a ranking that does not use
the comparable peer objects. Fabrication and citation quality need a person to read the answers;
the report lists every question that needs that review. It changes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def cli(args: list[str], profile: str | None) -> dict:
    p = subprocess.run(
        ["databricks", "genie", *args, "-o", "json"] + (["--profile", profile] if profile else []),
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise SystemExit(f"databricks genie {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:500]}")
    return json.loads(p.stdout)


def generated_sql(detail: dict) -> str:
    return "\n".join(
        r.get("response", "") for r in detail.get("actual_response") or [] if r.get("response_type") == "SQL"
    )


def structural_flags(q: dict, sql: str) -> list[str]:
    s = sql.lower()
    flags = []
    asks_for_us = re.search(r"\b(we|our|us)\b", q["question"].lower()) and q["category"] not in (
        "missing_data",
        "unsupported_jurisdiction",
        "non_comparable",
    )
    if (
        "company_scope" in q["critical"]
        and asks_for_us
        and s
        and not ("is_configured_company" in s or "deployment_context" in s)
    ):
        flags.append("company_scope: 'we/our' question did not read the configured company")
    if (
        "comparability" in q["critical"]
        and q["category"] == "peers_rank"
        and s
        and not ("vw_peer_comparison" in s or "mv_peer_comparison" in s or "is_comparable" in s)
    ):
        flags.append("comparability: ranking did not use the comparable peer objects")
    return flags


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--space-id", required=True)
    p.add_argument("--eval-run-id", required=True)
    p.add_argument("--profile")
    p.add_argument("--report", type=Path)
    args = p.parse_args(argv)
    bench = yaml.safe_load((ROOT / "eval" / "benchmark_questions.yaml").read_text(encoding="utf-8"))
    by_id = {f"6{int(q['id'].lstrip('b')):031d}": q for q in bench["questions"]}
    run = cli(["genie-get-eval-run", args.space_id, args.eval_run_id], args.profile)
    results, token = [], None
    while True:
        page = cli(
            ["genie-list-eval-results", args.space_id, args.eval_run_id] + (["--page-token", token] if token else []),
            args.profile,
        )
        results += page.get("eval_results") or []
        token = page.get("next_page_token")
        if not token:
            break
    rows = []
    for r in results:
        q = by_id.get(
            r["benchmark_question_id"], {"id": "?", "category": "?", "critical": [], "question": r["question"]}
        )
        d = cli(["genie-get-eval-result-details", args.space_id, args.eval_run_id, r["result_id"]], args.profile)
        sql = generated_sql(d)
        rows.append(
            {
                "id": q["id"],
                "category": q["category"],
                "question": r["question"],
                "assessment": d.get("assessment") or r.get("status"),
                "flags": structural_flags(q, sql),
                "generated_sql": sql,
            }
        )
    rows.sort(key=lambda x: x["id"])
    total = len(rows)
    good = sum(r["assessment"] == "GOOD" for r in rows)
    review = [r for r in rows if r["assessment"] == "NEEDS_REVIEW"]
    critical = [r for r in rows if r["flags"]]
    rate = good / total if total else 0.0
    gate = bench["gates"]["min_pass_rate"]
    for r in rows:
        print(f"{r['id']} {r['category']:<26} {r['assessment']:<13} {'; '.join(r['flags'])}")
    print(f"\nrun {args.eval_run_id}: status {run.get('eval_run_status')}, {total} questions")
    print(
        f"pass rate {good}/{total} = {rate:.0%} (gate {gate:.0%}); needs review {len(review)} (counted as not passed); "
        f"automatic critical flags {len(critical)}"
    )
    passed = rate >= gate and not critical
    print("GATES PASSED (pending human review of NEEDS_REVIEW and SME sign-off)" if passed else "GATES NOT PASSED")
    if args.report:
        args.report.write_text(json.dumps({"run": run, "pass_rate": rate, "results": rows}, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
