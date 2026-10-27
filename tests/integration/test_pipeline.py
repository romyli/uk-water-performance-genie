"""End-to-end pipeline behaviour on Spark, in the deployment's isolated test schema.

Run by the validate_project job. Each run drops every table in the test schema first, writes
generated fixtures under a run-specific folder of the validation Volume, and never calls a
publisher. Ops, Silver and Gold objects all live in the test schema for these tests.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from fakes import write_wcpr_csv
from water_performance import manifest, migrations, runtime, store, tasks

pytestmark = pytest.mark.integration

ASSET = "wcpr_2023_24_normalised_csv"  # a registered, enabled OFWAT_WCPR asset


@pytest.fixture(scope="module")
def spark():
    if not os.environ.get("WP_TEST_TARGET"):
        pytest.skip("integration tests run in the validate_project job")
    return runtime.get_spark()


@pytest.fixture(scope="module")
def target(spark):
    base = runtime.target_from_json(os.environ["WP_TEST_TARGET"])
    root = f"{os.environ['WP_TEST_ARTIFACT_ROOT']}/volumes-{uuid.uuid4().hex[:8]}"
    t = base.for_test(root)
    schema = t.schema("test")
    for r in spark.sql(f"SHOW TABLES IN {schema}").collect():
        name = f"{schema}.`{r['tableName']}`"
        try:
            spark.sql(f"DROP VIEW IF EXISTS {name}")
        except Exception:  # noqa: BLE001 - not a view
            spark.sql(f"DROP TABLE IF EXISTS {name}")
    return t


def _fetched(target, run_id, rows=None, header_offset=0):
    d = f"{target.raw_root}/OFWAT_WCPR/{ASSET}/run={run_id}"
    os.makedirs(d, exist_ok=True)
    path = write_wcpr_csv(f"{d}/wcpr.csv", header_offset=header_offset, rows=rows)
    sha, size = manifest.sha256_file(path)
    meta = {
        "retrieval_mode": "direct_asset",
        "asset_id": ASSET,
        "file_name": "wcpr.csv",
        "sha256": sha,
        "byte_size": size,
        "requested_url": "https://www.ofwat.gov.uk/wp-content/uploads/test.csv",
        "final_url": "https://www.ofwat.gov.uk/wp-content/uploads/test.csv",
        "status": 200,
        "landing_page": "https://www.ofwat.gov.uk/publication/test/",
        "release": "test",
        "retrieved_at": "2026-10-27T10:00:00+00:00",
    }
    with open(f"{d}/fetch.json", "w") as f:
        json.dump(meta, f)
    return sha


def _versions(spark, target):
    return store.rows(
        spark,
        f"SELECT asset_version, status, sha256 FROM {target.table('ops', 'asset_manifest')} "
        f"WHERE asset_key = '{ASSET}' ORDER BY asset_version",
    )


def test_migrations_upgrade_in_order_and_repeat_safely(spark, target):
    assert migrations.apply_migrations(spark, target, "it-mig", upto=3) == [1, 2, 3]
    wcpr = target.table("silver", "ofwat_wcpr_record")
    assert "reporting_area" not in spark.table(wcpr).columns
    spark.sql(
        f"INSERT INTO {wcpr} (source_id, asset_key, asset_version, sha256, record_locator, parser_version, "
        f"run_id, company_code, item_reference, report_year, value) VALUES ('OFWAT_WCPR', 'old', 1, 'x', 'r', "
        f"'p', 'it', 'TMS', 'APR3F_11', 2024, 1.0)"
    )
    assert migrations.apply_migrations(spark, target, "it-mig") == [4]
    assert spark.sql(f"SELECT reporting_area FROM {wcpr} WHERE asset_key = 'old'").collect()[0][0] == "whole_company"
    assert migrations.apply_migrations(spark, target, "it-mig") == []
    spark.sql(f"DELETE FROM {wcpr} WHERE asset_key = 'old'")


def test_inventory_skips_unchanged_content_and_versions_changes(spark, target):
    sha1 = _fetched(target, "it-run-1")
    assert tasks.inventory(spark, target, "it-run-1")["new"] == 1
    _fetched(target, "it-run-2")  # identical content on a rerun
    assert tasks.inventory(spark, target, "it-run-2")["unchanged"] == 1
    sha3 = _fetched(target, "it-run-3", header_offset=1)  # changed bytes
    assert tasks.inventory(spark, target, "it-run-3")["changed"] == 1
    v = _versions(spark, target)
    assert [(x["asset_version"], x["status"]) for x in v] == [(1, "superseded"), (2, "current")]
    assert v[0]["sha256"] == sha1 and v[1]["sha256"] == sha3
    archived = store.rows(
        spark, f"SELECT archive_path FROM {target.table('ops', 'asset_manifest')} WHERE asset_key = '{ASSET}'"
    )
    assert all(
        os.path.exists(a["archive_path"]) and f"sha256={x['sha256']}" in a["archive_path"] for a, x in zip(archived, v)
    )


def _refresh_after_parse(spark, target, run_id):
    tasks.parse(spark, target, run_id, "OFWAT_WCPR")
    tasks.conform(spark, target, run_id)
    tasks.publish(spark, target, run_id)
    tasks.quality(spark, target, run_id)


def test_incomplete_release_is_blocked_and_nothing_is_published(spark, target):
    _refresh_after_parse(spark, target, "it-run-3")
    gates = {
        r["check_id"]: r["status"]
        for r in store.rows(
            spark, f"SELECT check_id, status FROM {target.table('ops', 'quality_result')} WHERE release_id = 'it-run-3'"
        )
    }
    assert gates["completeness_OFWAT_WCPR"] == "fail" and gates["deployment_context_single_known_company"] == "pass"
    with pytest.raises(tasks.StageFailed, match="NOT published"):
        tasks.finalize(spark, target, "it-run-3")
    assert store.rows(spark, f"SELECT * FROM {target.table('ops', 'published_release')}") == []
    assert spark.sql(f"SELECT count(*) FROM {target.table('gold', 'deployment_context')}").collect()[0][0] == 0


def _relaxed(monkeypatch):
    cfg = tasks._quality_config()
    for c in cfg["completeness"].values():
        c.update({k: 0 for k in c})
    cfg["freshness"] = {}
    monkeypatch.setattr(tasks, "_quality_config", lambda: cfg)


def test_release_publishes_atomically_with_valid_grains(spark, target, monkeypatch):
    _relaxed(monkeypatch)
    _refresh_after_parse(spark, target, "it-run-4")
    assert tasks.finalize(spark, target, "it-run-4")["published"] == "it-run-4"
    g = lambda name: target.table("gold", name)  # noqa: E731
    one = lambda q: spark.sql(q).collect()[0][0]  # noqa: E731
    assert one(f"SELECT count(*) FROM {g('deployment_context')}") == 1
    assert one(f"SELECT company_code FROM {g('deployment_context')}") == target.company_code
    n = one(f"SELECT count(*) FROM {g('fact_reported_performance')}")
    assert n == one(f"SELECT count(DISTINCT observation_id) FROM {g('fact_reported_performance')}") > 0
    assert one(f"SELECT count(*) FROM {g('fact_reported_performance')} WHERE value = 0") == 0
    leak = store.rows(
        spark,
        f"SELECT metric_id, is_comparable, unit FROM {g('fact_reported_performance')} "
        f"WHERE source_item_reference = 'APR3F_11' LIMIT 1",
    )[0]
    assert leak["metric_id"] == "leakage_ml_per_day" and leak["is_comparable"] is False
    prov = store.rows(spark, f"SELECT sha256, asset_url, licence_id FROM {g('vw_provenance')} LIMIT 1")[0]
    assert prov["sha256"] and prov["licence_id"] == "OGL-UK-3.0"
    assert one(f"SELECT count(*) FROM {g('source_attribution')}") >= 1


def test_failed_parse_keeps_the_published_release_and_rollback_switches_back(spark, target, monkeypatch):
    _relaxed(monkeypatch)
    # A later release that changes one value, then publish it.
    _fetched(
        target, "it-run-5", rows=[["Thames Water", "TMS", "APR3F_11", "Leakage", "3F.5", "2023-24", 500.0, "Ml/day"]]
    )
    tasks.inventory(spark, target, "it-run-5")
    _refresh_after_parse(spark, target, "it-run-5")
    tasks.finalize(spark, target, "it-run-5")
    # A corrupt asset: the contract rejects it and the published release does not change.
    d = f"{target.raw_root}/OFWAT_WCPR/{ASSET}/run=it-run-6"
    os.makedirs(d, exist_ok=True)
    with open(f"{d}/wcpr.csv", "w") as f:
        f.write("not,the,layout\n1,2,3\n")
    sha, size = manifest.sha256_file(f"{d}/wcpr.csv")
    with open(f"{d}/fetch.json", "w") as f:
        json.dump({"retrieval_mode": "direct_asset", "file_name": "wcpr.csv", "sha256": sha, "byte_size": size}, f)
    tasks.inventory(spark, target, "it-run-6")
    tasks.parse(spark, target, "it-run-6", "OFWAT_WCPR")
    log = store.rows(spark, f"SELECT status FROM {target.table('ops', 'parse_log')} WHERE run_id = 'it-run-6'")
    assert [x["status"] for x in log] == ["quarantined"]

    def published():
        return store.rows(spark, f"SELECT release_id FROM {target.table('ops', 'published_release')}")[0]["release_id"]

    assert published() == "it-run-5"
    # The next refresh still publishes the last valid WCPR version, not an empty source.
    tasks.conform(spark, target, "it-run-6")
    tasks.publish(spark, target, "it-run-6")
    tasks.quality(spark, target, "it-run-6")
    tasks.finalize(spark, target, "it-run-6")
    assert published() == "it-run-6"
    leak = store.rows(
        spark,
        f"SELECT value FROM {target.table('gold', 'fact_reported_performance')} "
        f"WHERE source_item_reference = 'APR3F_11' AND company_code = 'TMS'",
    )
    assert [x["value"] for x in leak] == [500.0]
    tasks.rollback(spark, target, "it-rollback", release_id="it-run-4")
    assert published() == "it-run-4"
    statuses = {
        r["release_id"]: r["status"]
        for r in store.rows(spark, f"SELECT release_id, status FROM {target.table('ops', 'release')}")
    }
    assert statuses["it-run-4"] == "published" and statuses["it-run-6"] == "rolled_back"
