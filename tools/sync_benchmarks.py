#!/usr/bin/env python3
"""Copy eval/benchmark_questions.yaml into the Genie space's native benchmarks.

    uv run --with pyyaml python tools/sync_benchmarks.py           # rewrite the space file
    uv run --with pyyaml python tools/sync_benchmarks.py --check   # fail if they differ

Question IDs are derived from the benchmark IDs (b01 -> 600...01), so reruns are stable. The
repository tests run the same comparison in the validation job.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = ROOT / "eval" / "benchmark_questions.yaml"
SPACE = ROOT / "bundles" / "genie" / "src" / "water_performance.geniespace.json"


def native_benchmarks(doc: dict) -> dict:
    questions = []
    for q in doc["questions"]:
        number = int(q["id"].lstrip("b"))
        sql = [line + "\n" for line in q["sql"].strip().splitlines()]
        sql[-1] = sql[-1].rstrip("\n")
        questions.append(
            {
                "id": f"6{number:031d}",
                "question": [q["question"]],
                "answer": [{"format": "SQL", "content": sql}],
            }
        )
    return {"questions": sorted(questions, key=lambda x: x["id"])}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    doc = yaml.safe_load(BENCHMARKS.read_text(encoding="utf-8"))
    space = json.loads(SPACE.read_text(encoding="utf-8"))
    wanted = native_benchmarks(doc)
    if args.check:
        if space.get("benchmarks") != wanted:
            print(
                "benchmarks in the Genie space differ from eval/benchmark_questions.yaml; run tools/sync_benchmarks.py"
            )
            return 1
        print(f"{len(wanted['questions'])} benchmark questions in sync")
        return 0
    space["benchmarks"] = wanted
    SPACE.write_text(json.dumps(space, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(wanted['questions'])} benchmark questions to {SPACE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
