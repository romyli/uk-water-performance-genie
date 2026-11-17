"""Retrieve public source assets into the raw Volume.

Fetch tasks only write: each retrieval lands in
`<raw>/<source_id>/<asset_key>/run=<run_id>/` with a `fetch.json` that records what was asked for,
what came back and its checksum. The inventory stage decides whether that content is new and
archives it. A failed or blocked retrieval writes nothing that could replace the last valid release.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass

from . import manifest
from .http import HttpClient, RetrievalBlocked, RetrievalError
from .registry import Asset, Registry, StreamItem

LOG = logging.getLogger("water_performance.acquire")
FETCH_META = "fetch.json"

CC_BY_4 = re.compile(r"creativecommons\.org/licenses/by/4\.0|\bCC[ -]BY[ -]4\.0\b", re.IGNORECASE)
RAG_VERSION = re.compile(r"RAG\)?\s*(4\.\d{1,2})", re.IGNORECASE)


@dataclass
class FetchOutcome:
    source_id: str
    asset_key: str
    outcome: str  # fetched | unchanged_by_metadata | blocked | licence_rejected | failed | skipped
    message: str = ""
    metadata: dict | None = None
    raw_dir: str | None = None


def _iso(ms: int | None) -> str | None:
    if ms in (None, 0):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".partial"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def run_dir(raw_root: str, source_id: str, asset_key: str, run_id: str) -> str:
    return (
        f"{raw_root}/{manifest.safe_name(source_id)}/{manifest.safe_name(asset_key)}/run={manifest.safe_name(run_id)}"
    )


def strip_html(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


# --- Stream (ArcGIS) ---------------------------------------------------------------------------


def stream_licence(item_meta: dict) -> tuple[str | None, str, str | None]:
    """(licence_id or None, licence text, RAG version) from an ArcGIS item's metadata."""
    text = strip_html(item_meta.get("licenseInfo"))
    raw_html = item_meta.get("licenseInfo") or ""
    lic = "CC-BY-4.0" if (CC_BY_4.search(raw_html) or CC_BY_4.search(text)) else None
    rag = RAG_VERSION.search(text)
    return lic, text, rag.group(1) if rag else None


def discover_stream(client: HttpClient, registry: Registry, discovery_url: str) -> list[dict]:
    """Every public Stream item whose title says Annual Performance Report, registered or not."""
    registered = {i.item_id for i in registry.stream_items}
    found, start = [], 1
    while True:
        url = f"{discovery_url}?q={urllib.parse.quote('annual performance report')}&limit=100&startindex={start}"
        payload, _ = client.get_json(url)
        feats = payload.get("features") or []
        for f in feats:
            p = f.get("properties") or {}
            title = (p.get("title") or "").strip()
            if not re.search(r"annual[ _]performance[ _]report", title, re.IGNORECASE):
                continue
            found.append(
                {
                    "candidate_id": f.get("id"),
                    "title": title,
                    "licence_text": p.get("license"),
                    "registered": f.get("id") in registered,
                }
            )
        returned = payload.get("numberReturned") or len(feats)
        if not feats or start + returned > (payload.get("numberMatched") or 0):
            break
        start += returned
    return found


def fetch_stream_item(
    client: HttpClient, registry: Registry, item: StreamItem, raw_root: str, run_id: str, previous: dict | None
) -> FetchOutcome:
    """Snapshot one Stream item. Skips the download when the item and its data are unchanged."""
    api = registry.source("STREAM_APR")["api"]
    key = item.item_id
    try:
        meta, _ = client.get_json(api["item_metadata_url"].format(item_id=item.item_id))
        lic, lic_text, rag_version = stream_licence(meta)
        base = {
            "item_id": item.item_id,
            "item_title": meta.get("title"),
            "item_modified": _iso(meta.get("modified")),
            "item_owner": meta.get("owner"),
            "access": meta.get("access"),
            "licence_text": lic_text,
            "licence_id": lic,
            "rag_version": rag_version,
            "item_type": item.item_type,
            "company_code": item.company_code,
            "financial_year": item.financial_year,
            "reporting_area": item.reporting_area,
            "landing_page": f"https://www.streamwaterdata.co.uk/datasets/{item.item_id}",
        }
        if meta.get("access") != "public":
            return FetchOutcome("STREAM_APR", key, "licence_rejected", "item is not public", base)
        if lic != "CC-BY-4.0" and item.licence_override:
            base["licence_override"] = item.licence_override  # the operator loads it without an open licence
        elif lic != "CC-BY-4.0":
            return FetchOutcome(
                "STREAM_APR", key, "licence_rejected", f"item licence is not CC BY 4.0: {lic_text[:300]!r}", base
            )
        if item.item_type == "csv":
            return _fetch_stream_csv(client, api, item, base, raw_root, run_id, previous)
        return _fetch_stream_service(client, api, item, base, raw_root, run_id, previous)
    except RetrievalBlocked as e:
        return FetchOutcome("STREAM_APR", key, "blocked", str(e))
    except RetrievalError as e:
        return FetchOutcome("STREAM_APR", key, "failed", str(e))


def _same(a, b) -> bool:
    """Equal values, treating ISO timestamps with 'Z' or '+00:00' (and numbers as text) alike."""
    if a is None or b is None:
        return False
    try:  # timestamps: the manifest keeps whole seconds, ArcGIS reports milliseconds
        ta, tb = (dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")) for v in (a, b))
        return abs((ta - tb).total_seconds()) < 1
    except (TypeError, ValueError):
        pass
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _unchanged(previous: dict | None, **observed) -> bool:
    if not previous:
        return False
    return all(_same(previous.get(k), v) for k, v in observed.items())


def _fetch_stream_csv(client, api, item, base, raw_root, run_id, previous) -> FetchOutcome:
    if _unchanged(previous, item_modified=base["item_modified"]):
        return FetchOutcome(
            "STREAM_APR",
            item.item_id,
            "unchanged_by_metadata",
            "item modified time is unchanged since the archived version",
            base,
        )
    resp = client.get(api["item_data_url"].format(item_id=item.item_id))
    disp = resp.headers.get("content-disposition") or ""
    m = re.search(r'filename="?([^";]+)', disp)
    name = manifest.safe_name(m.group(1) if m else f"{item.item_id}.csv")
    if not name.lower().endswith(".csv"):
        name += ".csv"
    out = run_dir(raw_root, "STREAM_APR", item.item_id, run_id)
    _write(f"{out}/{name}", resp.body)
    meta = {
        **base,
        **resp.metadata(),
        "retrieval_mode": "api",
        "file_name": name,
        "byte_size": len(resp.body),
        "sha256": manifest.sha256_bytes(resp.body),
        "retrieved_at": dt.datetime.fromtimestamp(resp.retrieved_at, dt.timezone.utc).isoformat(),
    }
    _write(f"{out}/{FETCH_META}", json.dumps(meta, indent=2).encode())
    return FetchOutcome("STREAM_APR", item.item_id, "fetched", f"{len(resp.body)} bytes", meta, out)


def _fetch_stream_service(client, api, item, base, raw_root, run_id, previous) -> FetchOutcome:
    layer_url = f"{item.service_url}/{item.layer_id}"
    layer, _ = client.get_json(f"{layer_url}?f=json")
    oid = layer.get("objectIdField") or next(
        (f["name"] for f in layer.get("fields") or [] if f.get("type") == "esriFieldTypeOID"), "ObjectId"
    )
    edit = layer.get("editingInfo") or {}
    count_payload, _ = client.get_json(f"{layer_url}/query?where=1%3D1&returnCountOnly=true&f=json")
    expected = int(count_payload.get("count") or 0)
    base = {
        **base,
        "service_url": item.service_url,
        "layer_id": item.layer_id,
        "layer_name": layer.get("name"),
        "object_id_field": oid,
        "data_last_edit": _iso(edit.get("dataLastEditDate")),
        "schema_last_edit": _iso(edit.get("schemaLastEditDate")),
        "expected_record_count": expected,
        "fields": [{"name": f.get("name"), "type": f.get("type")} for f in layer.get("fields") or []],
    }
    if _unchanged(
        previous, item_modified=base["item_modified"], data_last_edit=base["data_last_edit"], api_record_count=expected
    ):
        return FetchOutcome(
            "STREAM_APR",
            item.item_id,
            "unchanged_by_metadata",
            "item, data edit time and record count are unchanged",
            base,
        )
    page_size = int(min(api.get("page_size", 1000), layer.get("maxRecordCount") or 1000))
    records, last, pages = [], -1, 0
    while True:  # keyset pagination on the object ID: stable even if the service reorders results
        where = urllib.parse.quote(f"{oid} > {last}")
        url = (
            f"{layer_url}/query?where={where}&outFields=*&orderByFields={oid}%20ASC"
            f"&resultRecordCount={page_size}&returnGeometry=false&f=json"
        )
        payload, _ = client.get_json(url)
        batch = [f.get("attributes") or {} for f in payload.get("features") or []]
        pages += 1
        if not batch:
            break
        records.extend(batch)
        last = max(int(r[oid]) for r in batch)
        if len(batch) < page_size and not payload.get("exceededTransferLimit"):
            break
    if len(records) != expected:
        raise RetrievalError(f"{item.item_id}: retrieved {len(records)} records but the service reports {expected}")
    if len({r[oid] for r in records}) != len(records):
        raise RetrievalError(f"{item.item_id}: duplicate object IDs in the snapshot")
    body = manifest.canonical_records(records, oid)
    out = run_dir(raw_root, "STREAM_APR", item.item_id, run_id)
    name = "records.jsonl.gz"
    _write(f"{out}/{name}", manifest.gzip_deterministic(body))
    meta = {
        **base,
        "retrieval_mode": "api",
        "final_url": layer_url,
        "file_name": name,
        "page_count": pages,
        "record_count": len(records),
        "byte_size": len(body),
        "sha256": manifest.sha256_bytes(body),
        "sha256_basis": "canonical JSON Lines of all records ordered by object ID (uncompressed)",
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _write(f"{out}/{FETCH_META}", json.dumps(meta, indent=2).encode())
    return FetchOutcome("STREAM_APR", item.item_id, "fetched", f"{len(records)} records in {pages} pages", meta, out)


# --- Direct public assets ---------------------------------------------------------------------


def fetch_direct_asset(client: HttpClient, asset: Asset, raw_root: str, run_id: str) -> FetchOutcome:
    try:
        resp = client.get(asset.url)
    except RetrievalBlocked as e:
        return FetchOutcome(asset.source_id, asset.asset_id, "blocked", manual_bootstrap_instruction(asset, str(e)))
    except RetrievalError as e:
        return FetchOutcome(asset.source_id, asset.asset_id, "failed", str(e))
    name = manifest.safe_name(urllib.parse.unquote(asset.url.rsplit("/", 1)[-1]))
    if resp.content_type == "text/html" and not name.lower().endswith((".html", ".htm")):
        return FetchOutcome(
            asset.source_id,
            asset.asset_id,
            "blocked",
            manual_bootstrap_instruction(asset, "the publisher returned an HTML page, not the asset"),
        )
    out = run_dir(raw_root, asset.source_id, asset.asset_id, run_id)
    _write(f"{out}/{name}", resp.body)
    meta = {
        **resp.metadata(),
        "retrieval_mode": "direct_asset",
        "asset_id": asset.asset_id,
        "release": asset.release,
        "published": asset.published,
        "landing_page": asset.landing_page,
        "file_name": name,
        "byte_size": len(resp.body),
        "sha256": manifest.sha256_bytes(resp.body),
        "retrieved_at": dt.datetime.fromtimestamp(resp.retrieved_at, dt.timezone.utc).isoformat(),
    }
    _write(f"{out}/{FETCH_META}", json.dumps(meta, indent=2).encode())
    return FetchOutcome(asset.source_id, asset.asset_id, "fetched", f"{len(resp.body)} bytes", meta, out)


def manual_bootstrap_instruction(asset: Asset, reason: str) -> str:
    return (
        f"Automated retrieval of {asset.source_id}/{asset.asset_id} is blocked ({reason}). Do not work around the "
        f"publisher's check. Open {asset.landing_page} in a browser, download {asset.url}, then run from the "
        f"repository root: python3 tools/source_upload.py --target <target> --source-id {asset.source_id} "
        f"--asset-id {asset.asset_id} --file <downloaded file> --landing-page {asset.landing_page} "
        f'--asset-url {asset.url} --release "{asset.release}" --published <publication date> --dry-run; '
        "repeat without --dry-run, then rerun refresh_performance."
    )


def manual_uploads(raw_root: str) -> list[tuple[str, dict]]:
    """Every operator-supplied file under <raw>/manual/<source_id>/<asset_id>/ with its sidecar."""
    base = f"{raw_root}/manual"
    found = []
    if not os.path.isdir(base):
        return found
    for dirpath, _, files in os.walk(base):
        for f in sorted(files):
            if f.endswith(manifest.BOOTSTRAP_SUFFIX):
                continue
            path = f"{dirpath}/{f}"
            side = path + manifest.BOOTSTRAP_SUFFIX
            meta = None
            if os.path.exists(side):
                with open(side, encoding="utf-8") as fh:
                    try:
                        meta = json.load(fh)
                    except json.JSONDecodeError:
                        meta = {"_invalid_json": True}
            found.append((path, meta))
    return found
