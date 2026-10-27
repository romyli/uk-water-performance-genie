"""The Genie retarget mutator: exact boundaries, specificity, no chaining and stale references."""

from __future__ import annotations

import pytest

import genie_retarget as gr

A = "uk_water_performance_genie.water_performance_gold"
RULES = {
    A: "cust_cat.wp_gold",
    "uk_water_performance_genie.water_performance_landing.curated_documents": "cust_cat.wp_landing.docs",
}


def r(text, rules=None):
    return gr.retarget_text(text, gr.parse_rules(rules or RULES))


def test_rewrites_names_in_sql_and_text():
    assert r(f"SELECT * FROM {A}.vw_reported_performance") == "SELECT * FROM cust_cat.wp_gold.vw_reported_performance"
    assert r(f"join {A}.dim_company.company_code = x") == "join cust_cat.wp_gold.dim_company.company_code = x"


def test_preserves_backtick_quoting_per_part():
    src = "`uk_water_performance_genie`.`water_performance_gold`.`fact_odi`"
    assert r(src) == "`cust_cat`.`wp_gold`.`fact_odi`"


def test_identifier_boundaries_are_exact():
    for text in (
        f"x{A}.t",
        f"a.{A}.t",
        f"{A}_old.t",
        f"`{A}x`.t",
        "uk_water_performance_genie_v2.water_performance_gold.t",
    ):
        assert r(text) == text, text


def test_ambiguous_short_names_are_not_rewritten():
    # Bare object or schema names carry no catalog, so they are left exactly as written.
    for text in (
        "vw_reported_performance.report_year",
        "water_performance_gold.vw_reported_performance",
        "dim_company",
        "FROM deployment_context",
    ):
        assert r(text) == text


def test_most_specific_rule_wins_and_rules_never_chain():
    rules = {A: "b_cat.b_gold", f"{A}.fact_odi": "c_cat.c_gold.fact_odi_v2", "b_cat.b_gold": "z_cat.z_gold"}
    assert r(f"{A}.fact_odi", rules) == "c_cat.c_gold.fact_odi_v2"
    assert r(f"{A}.dim_company", rules) == "b_cat.b_gold.dim_company"  # not rewritten again to z_cat


def test_volume_paths():
    src = "Read /Volumes/uk_water_performance_genie/water_performance_landing/curated_documents/rag/RAG-4.14.pdf"
    assert r(src) == "Read /Volumes/cust_cat/wp_landing/docs/rag/RAG-4.14.pdf"
    other = "/Volumes/uk_water_performance_genie/water_performance_landing/raw/x"
    assert r(other) == other


def test_rule_parts_must_match():
    with pytest.raises(gr.RetargetError):
        gr.parse_rules({A: "only_catalog"})
    with pytest.raises(gr.RetargetError):
        gr.parse_rules({"bad name.x": "a.b"})


def _space():
    return {
        "version": 2,
        "data_sources": {
            "tables": [{"identifier": f"{A}.vw_reported_performance"}, {"identifier": f"{A}.deployment_context"}],
            "metric_views": [{"identifier": f"{A}.mv_peer_comparison"}],
        },
        "instructions": {
            "text_instructions": [{"id": "1" * 32, "content": [f"Read {A}.deployment_context first."]}],
            "example_question_sqls": [
                {
                    "id": "2" * 32,
                    "question": ["q"],
                    "sql": ["SELECT * FROM `uk_water_performance_genie`.`water_performance_gold`.`fact_odi`"],
                }
            ],
        },
    }


def test_space_is_retargeted_sorted_and_free_of_stale_names():
    out = gr.retarget_space(_space(), RULES)
    ids = [t["identifier"] for t in out["data_sources"]["tables"]]
    assert ids == sorted(ids) == ["cust_cat.wp_gold.deployment_context", "cust_cat.wp_gold.vw_reported_performance"]
    assert "uk_water_performance_genie" not in str(out)


def test_same_catalog_different_schema_is_checked_for_stale_names():
    out = gr.retarget_space(_space(), {A: "uk_water_performance_genie.dev_me_water_performance_gold"})
    assert f"{A}." not in str(out) and "dev_me_water_performance_gold.deployment_context" in str(out)


def test_stale_reference_detection():
    rules = gr.parse_rules({A: "cust_cat.wp_gold"})
    assert gr.stale_references({"x": f"see {A}.t"}, rules) == [f"{A}"]
    assert gr.stale_references({"x": "see cust_cat.wp_gold.t"}, rules) == []


def test_identity_mapping_is_allowed():
    out = gr.retarget_space(_space(), {A: A})
    assert out["data_sources"]["tables"][0]["identifier"].startswith(A)
