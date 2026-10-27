"""Retrieval, checksums, versioning and manual bootstrap. No network: publishers are faked."""

from __future__ import annotations

import gzip
import json

import pytest

from fakes import FakeArcGIS, FakeTransport, client, json_response, stream_records
from water_performance import acquire, manifest, registry
from water_performance.http import HttpClient, RetrievalBlocked, RetrievalError

SERVICE = "https://services.example.test/arcgis/rest/services/APR/FeatureServer"


def _item(item_id="0" * 32, item_type="feature_service", status="enabled"):
    return registry.StreamItem(
        item_id=item_id,
        title="t",
        company_code="UUW",
        source_company_code="UUW",
        financial_year="2024-25",
        reporting_area="whole_company",
        item_type=item_type,
        status=status,
        service_url=SERVICE,
        layer_id=0,
    )


# --- HTTP ---------------------------------------------------------------------------------------


def test_retries_on_rate_limit_and_honours_retry_after():
    waits, seq = [], iter([429, 503, 200])
    t = FakeTransport().on(
        lambda u: True, lambda u: (next(seq), u, {"retry-after": "7", "content-type": "text/csv"}, b"a,b")
    )
    c = HttpClient(transport=t, sleep=waits.append, min_interval_seconds=0)
    resp = c.get("https://example.test/a.csv")
    assert resp.status == 200 and resp.attempts == 3
    assert waits == [7.0, 7.0]


def test_gives_up_after_max_attempts():
    t = FakeTransport().on(lambda u: True, lambda u: (500, u, {}, b""))
    with pytest.raises(RetrievalError, match="after 5 attempts"):
        client(t).get("https://example.test/x")


def test_browser_check_is_reported_as_blocked_not_retried():
    page = b"<html><title>Just a moment...</title>checking your browser</html>"
    t = FakeTransport().on(lambda u: True, lambda u: (403, u, {"content-type": "text/html"}, page))
    with pytest.raises(RetrievalBlocked, match="manual bootstrap"):
        client(t).get("https://www.ofwat.gov.uk/publication/x/")
    assert len(t.calls) == 1


def test_arcgis_error_in_200_body_is_retried():
    seq = iter([{"error": {"code": 429, "message": "Too many requests"}}, {"count": 3}])
    t = FakeTransport().on(
        lambda u: True, lambda u: (200, u, {"content-type": "application/json"}, json.dumps(next(seq)).encode())
    )
    payload, _ = client(t).get_json("https://example.test/query?f=json")
    assert payload == {"count": 3}


# --- Stream API snapshots -----------------------------------------------------------------------


def _registry():
    return registry.load_registry()


def test_stream_snapshot_pages_by_object_id_and_hashes_deterministically(artifact_dir):
    records = stream_records(7)
    t = FakeTransport()
    fake = FakeArcGIS(records, max_records=3).install(t, "0" * 32, SERVICE)
    out = acquire.fetch_stream_item(client(t), _registry(), _item(), str(artifact_dir), "run1", None)
    assert out.outcome == "fetched", out.message
    assert out.metadata["record_count"] == 7 and out.metadata["page_count"] == 3 and fake.queries == 3
    with gzip.open(f"{out.raw_dir}/records.jsonl.gz", "rt") as f:
        lines = f.read().splitlines()
    assert [json.loads(x)["ObjectId"] for x in lines] == list(range(1, 8))
    reordered = manifest.canonical_records(list(reversed(records)), "ObjectId")
    assert manifest.sha256_bytes(reordered) == out.metadata["sha256"]
    assert out.metadata["licence_id"] == "CC-BY-4.0" and out.metadata["rag_version"] == "4.13"


def test_stream_count_mismatch_fails_instead_of_writing_a_partial_snapshot(artifact_dir):
    t = FakeTransport()
    FakeArcGIS(stream_records(4), max_records=10).install(t, "0" * 32, SERVICE)
    t.routes.insert(0, (lambda u: "returnCountOnly=true" in u, json_response({"count": 5})))
    out = acquire.fetch_stream_item(client(t), _registry(), _item(), str(artifact_dir), "run1", None)
    assert out.outcome == "failed" and "reports 5" in out.message
    assert not (artifact_dir / "STREAM_APR").exists()


def test_stream_item_without_cc_by_licence_is_rejected_and_not_downloaded(artifact_dir):
    t = FakeTransport()
    fake = FakeArcGIS(stream_records(2), licence_html="<p>N/A</p>").install(t, "0" * 32, SERVICE)
    out = acquire.fetch_stream_item(client(t), _registry(), _item(), str(artifact_dir), "run1", None)
    assert out.outcome == "licence_rejected" and fake.queries == 0


def test_stream_unchanged_metadata_skips_the_download(artifact_dir):
    t = FakeTransport()
    fake = FakeArcGIS(stream_records(3)).install(t, "0" * 32, SERVICE)
    first = acquire.fetch_stream_item(client(t), _registry(), _item(), str(artifact_dir), "run1", None)
    previous = {
        "item_modified": first.metadata["item_modified"],
        "data_last_edit": first.metadata["data_last_edit"],
        "api_record_count": 3,
    }
    fake.queries = 0
    second = acquire.fetch_stream_item(client(t), _registry(), _item(), str(artifact_dir), "run2", previous)
    assert second.outcome == "unchanged_by_metadata" and fake.queries == 0


def test_licence_text_parsing():
    lic, text, rag = acquire.stream_licence(
        {
            "licenseInfo": "Data has been produced in accordance with Ofwat's "
            "Regulatory Accounting Guidance (RAG) 4.12. Licensed under CC BY 4.0"
        }
    )
    assert (lic, rag) == ("CC-BY-4.0", "4.12") and "CC BY 4.0" in text
    assert acquire.stream_licence({"licenseInfo": "<p>N/A</p>"})[0] is None


# --- Direct assets ------------------------------------------------------------------------------


def _asset(url="https://www.ofwat.gov.uk/wp-content/uploads/2024/10/x.csv"):
    return registry.Asset(
        source_id="OFWAT_WCPR",
        asset_id="a1",
        release="r",
        url=url,
        landing_page="https://www.ofwat.gov.uk/publication/x/",
        status="enabled",
    )


def test_direct_asset_keeps_http_metadata_and_checksum(artifact_dir):
    body = b"COMPANY_NAME,VALUE\nThames Water,1\n"
    t = FakeTransport().on(
        lambda u: True,
        lambda u: (
            200,
            u + "?final",
            {"content-type": "text/csv", "etag": '"abc"', "last-modified": "Tue, 08 Oct 2024 10:00:00 GMT"},
            body,
        ),
    )
    out = acquire.fetch_direct_asset(client(t), _asset(), str(artifact_dir), "run1")
    assert out.outcome == "fetched"
    m = out.metadata
    assert m["sha256"] == manifest.sha256_bytes(body) and m["byte_size"] == len(body)
    assert m["final_url"].endswith("?final") and m["headers"]["etag"] == '"abc"' and m["retrieved_at"]


def test_html_instead_of_asset_produces_manual_bootstrap_instruction(artifact_dir):
    t = FakeTransport().on(lambda u: True, lambda u: (200, u, {"content-type": "text/html"}, b"<html>hello</html>"))
    out = acquire.fetch_direct_asset(client(t), _asset(), str(artifact_dir), "run1")
    assert out.outcome == "blocked"
    assert "tools/source_upload.py" in out.message and "Do not work around" in out.message
    assert not (artifact_dir / "OFWAT_WCPR").exists()


# --- Versioning ---------------------------------------------------------------------------------


def test_version_decisions():
    assert manifest.decide("a", []) == manifest.Decision("new", 1, None)
    rows = [
        {"asset_version": 1, "sha256": "a", "status": "superseded"},
        {"asset_version": 2, "sha256": "b", "status": "current"},
    ]
    assert manifest.decide("b", rows).action == "unchanged"
    assert manifest.decide("c", rows) == manifest.Decision("changed", 3, 2)
    # Reverting to old content is recognised, not ingested as a new release.
    assert manifest.decide("a", rows) == manifest.Decision("unchanged", 1, 1)


def test_gzip_is_deterministic_and_archive_path_is_content_addressed():
    assert manifest.gzip_deterministic(b"x" * 100) == manifest.gzip_deterministic(b"x" * 100)
    p = manifest.archive_relpath("OFWAT_WCPR", "a1", "f" * 64, "WCPR 2023-24.csv")
    assert p == f"OFWAT_WCPR/a1/sha256={'f' * 64}/WCPR_2023-24.csv"


# --- Manual bootstrap ---------------------------------------------------------------------------


def _meta(**over):
    m = {
        "source_id": "OFWAT_PR24_OUTCOMES",
        "asset_id": "a",
        "landing_page": "https://www.ofwat.gov.uk/p/",
        "asset_url": "https://www.ofwat.gov.uk/wp-content/a.xlsx",
        "release": "V9",
        "published": "2025-06",
        "licence_id": "OGL-UK-3.0",
        "licence_evidence": "OGL statement on the page",
        "supplied_by": "ops team",
        "downloaded_at": "2026-10-27T10:00:00Z",
        "sha256": "ab" * 32,
    }
    m.update(over)
    return m


def test_bootstrap_requires_traceable_metadata():
    lic = ("OGL-UK-3.0", "CC-BY-4.0")
    assert manifest.validate_bootstrap(_meta(), "ab" * 32, lic)
    with pytest.raises(manifest.BootstrapError, match="missing"):
        manifest.validate_bootstrap(_meta(landing_page=""), "ab" * 32, lic)
    with pytest.raises(manifest.BootstrapError, match="https"):
        manifest.validate_bootstrap(_meta(asset_url="file:///home/me/a.xlsx"), "ab" * 32, lic)
    with pytest.raises(manifest.BootstrapError, match="checksum mismatch"):
        manifest.validate_bootstrap(_meta(), "cd" * 32, lic)
    with pytest.raises(manifest.BootstrapError, match="not a recognised"):
        manifest.validate_bootstrap(_meta(licence_id="All rights reserved"), "ab" * 32, lic)


def test_manual_uploads_are_found_with_their_sidecars(artifact_dir):
    d = artifact_dir / "manual" / "OFWAT_PR24_OUTCOMES" / "a"
    d.mkdir(parents=True)
    (d / "a.xlsx").write_bytes(b"PK")
    (d / "a.xlsx.bootstrap.json").write_text(json.dumps(_meta()))
    (d / "b.xlsx").write_bytes(b"PK")
    found = dict(acquire.manual_uploads(str(artifact_dir)))
    assert found[str(d / "a.xlsx")]["release"] == "V9"
    assert found[str(d / "b.xlsx")] is None


def test_metadata_comparison_normalises_timestamp_formats():
    assert acquire._same("2025-12-11T11:41:54Z", "2025-12-11T11:41:54+00:00")
    assert acquire._same("10515", 10515)
    assert not acquire._same(None, None) and not acquire._same("2025-12-11T11:41:54Z", "2025-12-12T11:41:54Z")
    assert acquire._same("2026-06-22T08:59:14.766000+00:00", "2026-06-22T08:59:14Z")
