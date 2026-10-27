"""Validation job entry point: `run_validation.py <suite> --target <json> --run-id <id>`.

Suites:
  lint         ruff check and ruff format --check over the repository
  unit         deterministic tests (tests/unit and tests/repo); no network, generated fixtures
  integration  tests against isolated tables in the test schema (tests/integration)

Reports go to the validation_reports Volume and the ops.test_result table. The task fails when
its suite fails.
"""

import argparse
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _here() -> str:
    return os.path.dirname(os.path.abspath(globals().get("__file__") or sys.argv[0]))


SRC = os.path.dirname(_here())
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SRC)))
sys.path.insert(0, SRC)

from water_performance import runtime, store  # noqa: E402


def run_lint(report_dir: str) -> tuple[int, list[dict]]:
    results = []
    for name, args in (
        ("ruff check", ["check", "--no-cache", "--output-format", "concise", "."]),
        ("ruff format", ["format", "--no-cache", "--check", "--diff", "."]),
    ):
        start = time.time()
        p = subprocess.run([sys.executable, "-m", "ruff", *args], cwd=ROOT, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        print(f"$ ruff {' '.join(args)}\n{out}\n")
        with open(os.path.join(report_dir, f"{name.replace(' ', '_')}.txt"), "w", encoding="utf-8") as f:
            f.write(out)
        results.append(
            {
                "test_id": name,
                "outcome": "passed" if p.returncode == 0 else "failed",
                "duration_seconds": time.time() - start,
                "message": out[-4000:],
            }
        )
    return (0 if all(r["outcome"] == "passed" for r in results) else 1), results


def run_pytest(paths: list[str], report_dir: str, suite: str, extra_env: dict) -> tuple[int, list[dict]]:
    import pytest

    os.environ.update(extra_env)
    junit = os.path.join(report_dir, f"{suite}.xml")
    os.chdir(ROOT)
    code = pytest.main(
        [
            *paths,
            "-p",
            "no:cacheprovider",
            "-ra",
            "-q",
            f"--junitxml={junit}",
            f"--rootdir={ROOT}",
            "-c",
            os.path.join(ROOT, "pyproject.toml"),
        ]
    )
    results = []
    if os.path.exists(junit):
        for case in ET.parse(junit).getroot().iter("testcase"):
            failure = case.find("failure") if case.find("failure") is not None else case.find("error")
            skipped = case.find("skipped")
            outcome = "failed" if failure is not None else "skipped" if skipped is not None else "passed"
            msg = (
                failure.get("message") if failure is not None else skipped.get("message") if skipped is not None else ""
            )
            results.append(
                {
                    "test_id": f"{case.get('classname')}::{case.get('name')}",
                    "outcome": outcome,
                    "duration_seconds": float(case.get("time") or 0),
                    "message": (msg or "")[:4000],
                }
            )
    return int(code), results


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("lint", "unit", "integration"))
    runtime.add_target_args(parser)
    args = parser.parse_args(argv)
    runtime.configure_logging()
    target = runtime.target_from_args(args)
    report_dir = f"{target.volume(target.reports_volume)}/run={args.run_id}/{args.suite}"
    os.makedirs(report_dir, exist_ok=True)
    env = {"WP_TEST_ARTIFACT_ROOT": report_dir, "WP_TEST_TARGET": args.target, "WP_TEST_RUN_ID": args.run_id}
    if args.suite == "lint":
        code, results = run_lint(report_dir)
    elif args.suite == "unit":
        code, results = run_pytest(["tests/unit", "tests/repo"], report_dir, "unit", env)
    else:
        code, results = run_pytest(["tests/integration"], report_dir, "integration", env)
    spark = runtime.get_spark()
    store.append(
        spark,
        target.table("ops", "test_result"),
        [{"run_id": args.run_id, "suite": args.suite, **r, "recorded_at": runtime.utcnow()} for r in results],
    )
    counts = {o: sum(r["outcome"] == o for r in results) for o in ("passed", "failed", "skipped")}
    print(f"{args.suite}: {counts}; reports in {report_dir}")
    if code != 0:
        raise SystemExit(f"{args.suite} failed: {counts}")


if __name__ == "__main__":
    main()
