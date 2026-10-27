"""The Genie space definition: valid structure, Gold-only sources, focus, and benchmark coverage."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import genie_retarget as gr

ROOT = Path(__file__).resolve().parents[2]
SPACE = json.loads((ROOT / "bundles/genie/src/water_performance.geniespace.json").read_text(encoding="utf-8"))
BENCH = yaml.safe_load((ROOT / "eval/benchmark_questions.yaml").read_text(encoding="utf-8"))
GOLD = "uk_water_performance_genie.water_performance_gold"
DOCS = "/Volumes/uk_water_performance_genie/water_performance_landing/curated_documents"
MAPPING = yaml.safe_load((ROOT / "bundles/genie/databricks.yml").read_text(encoding="utf-8"))["variables"][
    "genie_table_mapping"
]["default"]
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _objects():
    ds = SPACE["data_sources"]
    return [t["identifier"] for k in ("tables", "metric_views") for t in ds.get(k, [])]


def test_only_gold_objects_and_the_curated_documents_volume():
    for ident in _objects():
        assert ident.startswith(GOLD + "."), ident
    for v in SPACE["data_sources"].get("volumes", []):
        assert v["path"] == DOCS


def test_space_is_focused():
    assert 5 <= len(_objects()) <= 8
    assert any(i.endswith(".deployment_context") for i in _objects())
    assert SPACE["data_sources"]["metric_views"], "the peer metric view is attached"


def test_no_raw_ops_or_silver_references_anywhere():
    text = json.dumps(SPACE)
    for word in ("water_performance_ops", "water_performance_silver", "/raw/", "/archive/", "_silver.release_"):
        assert word not in text, word


def test_no_placeholders_or_environment_values():
    text = json.dumps(SPACE)
    assert "${" not in text and "{{" not in text
    assert not re.search(r"\b[0-9a-f]{16}\b", text), "no warehouse IDs"
    assert "Thames" not in text and "file_path" not in SPACE


def test_ids_and_ordering_follow_the_api_rules():
    ids = []
    for q in SPACE["config"]["sample_questions"]:
        ids.append(q["id"])
    for key in ("text_instructions", "example_question_sqls", "join_specs"):
        items = SPACE["instructions"].get(key, [])
        assert [i["id"] for i in items] == sorted(i["id"] for i in items), key
        ids += [i["id"] for i in items]
    for items in SPACE["instructions"].get("sql_snippets", {}).values():
        ids += [i["id"] for i in items]
    bq = SPACE["benchmarks"]["questions"]
    assert [q["id"] for q in bq] == sorted(q["id"] for q in bq)
    ids += [q["id"] for q in bq]
    assert all(HEX32.match(i) for i in ids) and len(ids) == len(set(ids))
    assert len(SPACE["instructions"]["text_instructions"]) == 1
    for key in ("tables", "metric_views"):
        idents = [t["identifier"] for t in SPACE["data_sources"][key]]
        assert idents == sorted(idents)
    for t in SPACE["data_sources"]["tables"]:
        names = [c["column_name"] for c in t.get("column_configs", [])]
        assert names == sorted(names)


def test_instructions_cover_the_required_behaviour():
    text = " ".join(SPACE["instructions"]["text_instructions"][0]["content"]).lower()
    for phrase in (
        "deployment_context",
        "we, us, our",
        "2024-25 = 2025",
        "pr19",
        "pr24",
        "measure()",
        "not available in the public sources held",
        "unit",
        "price base",
        "scotland",
        "cite",
        "comparability_reason",
        "name",
    ):
        assert phrase in text, phrase


def test_benchmarks_match_the_yaml():
    from sync_benchmarks import native_benchmarks

    assert SPACE["benchmarks"] == native_benchmarks(BENCH)


REQUIRED_CATEGORIES = {
    "configured_company_summary",
    "trend",
    "missed_targets",
    "target_vs_actual",
    "odi",
    "finance",
    "peers_rank",
    "customer",
    "environment",
    "drinking_water",
    "period_ambiguity",
    "explicit_company_override",
    "missing_data",
    "non_comparable",
    "unsupported_jurisdiction",
    "provenance",
}


def test_benchmark_coverage_and_gates():
    cats = {q["category"] for q in BENCH["questions"]}
    assert cats >= REQUIRED_CATEGORIES, REQUIRED_CATEGORIES - cats
    assert BENCH["gates"]["min_pass_rate"] == 0.70 and BENCH["gates"]["critical_failures_allowed"] == 0
    for q in BENCH["questions"]:
        assert q["sql"].strip() and q["critical"] and q["expected_behaviour"], q["id"]
        assert set(q["critical"]) <= {"company_scope", "comparability", "fabrication", "provenance"}
        assert not re.search(r"\b\d+\.\d+\b", q["expected_behaviour"]), "expected answers are SQL, not numbers"


@pytest.mark.parametrize("q", BENCH["questions"], ids=lambda q: q["id"])
def test_benchmark_sql_reads_gold_only(q):
    refs = re.findall(r"uk_water_performance_genie\.[a-z_]+\.[a-z_]+", q["sql"])
    assert refs and all(r.startswith(GOLD + ".") for r in refs)
    about_data_held = q["category"] in ("missing_data", "unsupported_jurisdiction", "non_comparable")
    if not about_data_held and ("configured" in q["category"] or re.search(r"\b(we|our|us)\b", q["question"].lower())):
        assert "is_configured_company" in q["sql"] or "deployment_context" in q["sql"], q["id"]


def test_space_retargets_cleanly_for_a_customer_target():
    mapping = {
        k: v.replace("${var.catalog}", "acme_water")
        .replace("${var.schema_prefix}", "")
        .replace("${var.gold_schema}", "wp_gold")
        .replace("${var.landing_schema}", "wp_landing")
        .replace("${var.documents_volume}", "docs")
        for k, v in MAPPING.items()
    }
    out = gr.retarget_space(SPACE, mapping)
    text = json.dumps(out)
    assert "uk_water_performance_genie" not in text
    assert all(
        i.startswith("acme_water.wp_gold.")
        for k in ("tables", "metric_views")
        for i in (t["identifier"] for t in out["data_sources"][k])
    )
    assert out["data_sources"]["volumes"][0]["path"] == "/Volumes/acme_water/wp_landing/docs"
