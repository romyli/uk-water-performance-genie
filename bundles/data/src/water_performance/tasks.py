"""Stage implementations for the refresh_performance job. Each function is one job task.

Task entry points (src/tasks/run_stage.py) only parse arguments and call these functions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor

from . import acquire, assets, manifest, migrations, periods, registry, store
from .http import HttpClient
from .runtime import Target, sql_literal, utcnow
from .tabular import ContractError

LOG = logging.getLogger("water_performance.tasks")
# Parallel Stream item retrievals. Each item still pages sequentially with its own request spacing.
STREAM_WORKERS = 6


class StageFailed(RuntimeError):
    """The stage finished its independent work but must fail the task."""


# --- setup -------------------------------------------------------------------------------------


def setup(spark, target: Target, run_id: str) -> dict:
    """Apply schema migrations, validate configuration and snapshot the source registry."""
    started = utcnow()
    applied = migrations.apply_migrations(spark, target, run_id)
    reg = registry.load_registry()
    companies = registry.load_companies()
    company = registry.resolve_company(target.company_code, companies)
    snap = []
    for s in reg.sources.values():
        failures = registry.gate_failures(s, reg.licences) if s.status != "hold" else [s.get("hold_reason")]
        snap.append(
            {
                "run_id": run_id,
                "source_id": s.source_id,
                "priority": s.get("priority"),
                "status": s.status,
                "effective_status": s.status if s.status == "hold" or not failures else "hold",
                "gate_failures": failures,
                "registry_entry": s.raw,
                "captured_at": started,
            }
        )
    store.append(spark, target.table("ops", "source_registry_snapshot"), snap)
    summary = {"migrations_applied": applied, "company": company.company_code, "sources": len(snap)}
    store.record_stage(spark, target, run_id, "setup", "succeeded", started, metrics=summary)
    return summary


# --- discover and fetch ------------------------------------------------------------------------


def discover(spark, target: Target, run_id: str, client: HttpClient | None = None) -> dict:
    started = utcnow()
    reg = registry.load_registry()
    client = client or HttpClient()
    api = reg.source("STREAM_APR")["api"]
    found = acquire.discover_stream(client, reg, api["discovery_url"])
    store.append(
        spark,
        target.table("ops", "source_candidate"),
        [{"run_id": run_id, "source_id": "STREAM_APR", **f, "discovered_at": started} for f in found],
    )
    unregistered = [f for f in found if not f["registered"]]
    msg = f"{len(found)} public Stream APR items found; {len(unregistered)} are not registered and were not ingested"
    store.record_stage(
        spark,
        target,
        run_id,
        "discover",
        "succeeded",
        started,
        "STREAM_APR",
        msg,
        {"found": len(found), "unregistered": [u["candidate_id"] for u in unregistered]},
    )
    return {"found": len(found), "unregistered": len(unregistered)}


def _latest_manifest(spark, target: Target, source_id: str) -> dict[str, dict]:
    rows = store.rows(
        spark,
        f"""
        SELECT asset_key, asset_version, sha256, api_record_count, status,
               date_format(api_item_modified, "yyyy-MM-dd'T'HH:mm:ssXXX") AS item_modified,
               date_format(api_data_last_edit, "yyyy-MM-dd'T'HH:mm:ssXXX") AS data_last_edit
        FROM {target.table('ops', 'asset_manifest')}
        WHERE source_id = '{source_id}' AND status = 'current'""",
    )
    return {r["asset_key"]: r for r in rows}


def fetch(spark, target: Target, run_id: str, source_id: str, client: HttpClient | None = None) -> dict:
    """Retrieve every enabled or conditional asset of one source into the raw Volume."""
    started = utcnow()
    reg = registry.load_registry()
    src = reg.source(source_id)
    outcomes: list[acquire.FetchOutcome] = []
    if src.status == "hold":
        outcomes.append(acquire.FetchOutcome(source_id, "*", "skipped", f"source is on hold: {src.get('hold_reason')}"))
    elif source_id == "STREAM_APR":
        previous = _latest_manifest(spark, target, source_id)
        for item in reg.stream_items:
            if item.status != "enabled":
                outcomes.append(
                    acquire.FetchOutcome(source_id, item.item_id, "skipped", f"item on hold: {item.hold_reason}")
                )

        def one(item):
            # One client per item: each keeps its own request spacing.
            c = client or HttpClient()
            o = acquire.fetch_stream_item(c, reg, item, target.raw_root, run_id, previous.get(item.item_id))
            LOG.info("%s %s: %s %s", source_id, item.item_id, o.outcome, o.message)
            return o

        enabled = [i for i in reg.stream_items if i.status == "enabled"]
        with ThreadPoolExecutor(max_workers=STREAM_WORKERS) as pool:
            outcomes.extend(pool.map(one, enabled))
    elif src["retrieval_mode"] == "direct_asset":
        for a in src.assets:
            if a.status == "hold":
                outcomes.append(acquire.FetchOutcome(source_id, a.asset_id, "skipped", "asset on hold"))
                continue
            outcomes.append(acquire.fetch_direct_asset(client or HttpClient(), a, target.raw_root, run_id))
    else:
        outcomes.append(
            acquire.FetchOutcome(
                source_id, "*", "skipped", "manual bootstrap source: upload assets with tools/source_upload.py"
            )
        )
    store.append(
        spark,
        target.table("ops", "retrieval_event"),
        [
            {
                "run_id": run_id,
                "source_id": o.source_id,
                "asset_key": o.asset_key,
                "outcome": o.outcome,
                "message": o.message[:8000],
                "metadata": o.metadata or {},
                "event_at": utcnow(),
            }
            for o in outcomes
        ],
    )
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    failed = [o for o in outcomes if o.outcome in ("failed", "blocked")]
    status = "succeeded" if not failed else "failed"
    message = "; ".join(f"{o.asset_key}: {o.outcome}: {o.message[:300]}" for o in failed) or json.dumps(counts)
    store.record_stage(spark, target, run_id, "fetch", status, started, source_id, message, counts)
    if failed and len(failed) == len([o for o in outcomes if o.outcome != "skipped"]):
        # Nothing new could be retrieved. Earlier archived versions remain current and are still used.
        raise StageFailed(f"{source_id}: every retrieval failed or was blocked: {message}")
    return counts


# --- inventory and archive ---------------------------------------------------------------------


def _manifest_versions(spark, target: Target) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for r in store.rows(
        spark,
        f"SELECT source_id, asset_key, asset_version, sha256, status " f"FROM {target.table('ops', 'asset_manifest')}",
    ):
        out.setdefault((r["source_id"], r["asset_key"]), []).append(r)
    return out


def _fetched_this_run(target: Target, run_id: str) -> list[tuple[str, str, str, dict]]:
    found = []
    root = target.raw_root
    if not os.path.isdir(root):
        return found
    run_folder = f"run={manifest.safe_name(run_id)}"
    for source_id in sorted(os.listdir(root)):
        if source_id == "manual" or not os.path.isdir(f"{root}/{source_id}"):
            continue
        for asset_key in sorted(os.listdir(f"{root}/{source_id}")):
            d = f"{root}/{source_id}/{asset_key}/{run_folder}"
            meta_path = f"{d}/{acquire.FETCH_META}"
            if os.path.exists(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    found.append((source_id, asset_key, d, json.load(f)))
    return found


def inventory(spark, target: Target, run_id: str) -> dict:
    """Checksum new retrievals and manual uploads, archive new versions, and update the manifest."""
    started = utcnow()
    reg = registry.load_registry()
    existing = _manifest_versions(spark, target)
    manifest_rows, rejects, profiles, evidence, superseded = [], [], [], [], []
    counts = {"new": 0, "changed": 0, "unchanged": 0, "quarantined": 0}

    candidates = []
    for source_id, asset_key, d, meta in _fetched_this_run(target, run_id):
        candidates.append((source_id, asset_key, f"{d}/{meta['file_name']}", meta, None))
    for path, side in acquire.manual_uploads(target.raw_root):
        rel = path[len(target.raw_root) + len("/manual/") :].split("/")
        if len(rel) < 3:
            rejects.append(
                _reject(
                    run_id,
                    "UNKNOWN",
                    path,
                    None,
                    "BOOTSTRAP_LAYOUT",
                    "manual uploads must be under manual/<source_id>/<asset_id>/<file>",
                )
            )
            continue
        candidates.append((rel[0], rel[1], path, side, "manual"))

    for source_id, asset_key, path, meta, mode in candidates:
        sha, size = manifest.sha256_file(path)
        if meta and meta.get("sha256_basis"):  # API snapshot: identity is the canonical record content
            sha = meta["sha256"]
        src = reg.sources.get(source_id)
        if src is None:
            rejects.append(_reject(run_id, source_id, path, None, "UNKNOWN_SOURCE", "source_id is not registered"))
            counts["quarantined"] += 1
            continue
        if mode == "manual":
            try:
                if meta is None:
                    raise manifest.BootstrapError(f"no {manifest.BOOTSTRAP_SUFFIX} sidecar next to the file")
                manifest.validate_bootstrap(meta, sha, reg.licences)
            except manifest.BootstrapError as e:
                rejects.append(_reject(run_id, source_id, path, None, "BOOTSTRAP_METADATA", str(e)))
                counts["quarantined"] += 1
                continue
        decision = manifest.decide(sha, existing.get((source_id, asset_key), []))
        if decision.action == manifest.UNCHANGED:
            counts["unchanged"] += 1
            spark.sql(
                f"UPDATE {target.table('ops', 'asset_manifest')} SET last_checked_run_id = '{run_id}' "
                f"WHERE source_id = '{source_id}' AND asset_key = '{asset_key}' "
                f"AND asset_version = {decision.version}"
            )
            continue
        file_name = os.path.basename(path)
        rel = manifest.archive_relpath(source_id, asset_key, sha, file_name)
        dest = f"{target.archive_root}/{rel}"
        if not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(path, dest + ".partial")
            os.replace(dest + ".partial", dest)
            with open(os.path.join(os.path.dirname(dest), "source.json"), "w", encoding="utf-8") as f:
                json.dump(meta or {}, f, indent=2, sort_keys=True, default=str)
        asset = next((a for a in src.assets if a.asset_id == asset_key), None)
        lic_id, lic_evidence, verified = _licence(src, asset, meta)
        if verified is None:  # the licence must be stated inside the asset itself
            found_id, statement, locator = assets.find_licence_statement(dest, file_name)
            verified = found_id is not None and found_id == src.licence_id
            lic_id = found_id
            lic_evidence = (
                f"{statement} [{locator}]" if statement else "No Open Government Licence statement found in the asset"
            )
            evidence.append(
                {
                    "source_id": source_id,
                    "asset_key": asset_key,
                    "asset_version": decision.version,
                    "sha256": sha,
                    "licence_id": found_id,
                    "statement": statement,
                    "locator": locator,
                    "verified": verified,
                    "run_id": run_id,
                }
            )
        if decision.previous_version is not None:
            superseded.append((source_id, asset_key, decision.previous_version, decision.version))
        counts[decision.action] += 1
        manifest_rows.append(
            _manifest_row(
                run_id,
                src,
                asset,
                asset_key,
                decision.version,
                meta or {},
                mode,
                file_name,
                size,
                sha,
                path,
                dest,
                lic_id,
                lic_evidence,
                verified,
            )
        )
        for p in assets.profile(dest, file_name):
            profiles.append(
                {
                    "source_id": source_id,
                    "asset_key": asset_key,
                    "asset_version": decision.version,
                    "part": p["part"],
                    "max_row": p["max_row"],
                    "max_col": p["max_col"],
                    "sample": p["sample"],
                    "profiled_at": utcnow(),
                }
            )

    for source_id, asset_key, _previous, new in superseded:
        spark.sql(
            f"UPDATE {target.table('ops', 'asset_manifest')} SET status = 'superseded', "
            f"superseded_by_version = {new}, status_reason = 'replaced by version {new}' "
            f"WHERE source_id = '{source_id}' AND asset_key = '{asset_key}' AND status = 'current'"
        )
    store.append(spark, target.table("ops", "asset_manifest"), manifest_rows)
    store.append(spark, target.table("ops", "asset_profile"), profiles)
    store.append(spark, target.table("ops", "reject"), rejects)
    store.append(spark, target.table("silver", "licence_evidence"), evidence)
    counts["licence_reverified"] = _reverify_licences(spark, target, reg, run_id, evidence_seen=evidence)
    withdrawn = _mark_withdrawn(spark, target, reg)
    counts["withdrawn"] = withdrawn
    status = "succeeded" if not rejects else "succeeded_with_rejects"
    store.record_stage(
        spark,
        target,
        run_id,
        "inventory",
        status,
        started,
        None,
        "; ".join(r["reason"] for r in rejects)[:4000],
        counts,
    )
    return counts


def _licence(src, asset, meta):
    """(licence_id, evidence, verified). verified=None means 'look inside the asset'."""
    if src.source_id == "STREAM_APR":
        return meta.get("licence_id"), meta.get("licence_text"), meta.get("licence_id") == "CC-BY-4.0"
    if meta.get("supplied_by"):  # manual bootstrap: the operator recorded the landing-page licence
        return meta["licence_id"], meta["licence_evidence"], meta["licence_id"] == src.licence_id
    if asset is not None and asset.licence_verification == "in_asset_statement":
        return None, None, None
    if asset is not None and asset.licence_verification == "operator_confirmed":
        return src.licence_id, (src.get("licence") or {}).get("evidence"), True
    return src.licence_id, (src.get("licence") or {}).get("evidence"), src.status == "enabled"


def _manifest_row(
    run_id,
    src,
    asset,
    asset_key,
    version,
    meta,
    mode,
    file_name,
    size,
    sha,
    raw_path,
    dest,
    lic_id,
    lic_evidence,
    verified,
) -> dict:
    is_api = meta.get("retrieval_mode") == "api" and meta.get("sha256_basis")
    return {
        "source_id": src.source_id,
        "asset_key": asset_key,
        "asset_version": version,
        "release_label": (asset.release if asset else None)
        or meta.get("release")
        or (f"{meta.get('financial_year')} ({meta.get('item_title')})" if meta.get("item_id") else None),
        "retrieval_mode": "manual_bootstrap"
        if mode == "manual"
        else meta.get("retrieval_mode") or src["retrieval_mode"],
        "landing_page": meta.get("landing_page") or (asset.landing_page if asset else src.get("landing_page")),
        "asset_url": meta.get("asset_url")
        or meta.get("requested_url")
        or (asset.url if asset else None)
        or meta.get("service_url"),
        "final_url": meta.get("final_url"),
        "api_item_id": meta.get("item_id"),
        "api_service_url": meta.get("service_url"),
        "api_layer_id": meta.get("layer_id"),
        "api_record_count": meta.get("record_count") or meta.get("expected_record_count"),
        "api_item_modified": meta.get("item_modified"),
        "api_data_last_edit": meta.get("data_last_edit"),
        "http_metadata": {
            k: meta.get(k)
            for k in (
                "status",
                "headers",
                "attempts",
                "page_count",
                "sha256_basis",
                "fields",
                "rag_version",
                "supplied_by",
                "downloaded_at",
            )
            if meta.get(k) is not None
        },
        "retrieved_at": meta.get("retrieved_at") or meta.get("downloaded_at"),
        "published": meta.get("published") or (asset.published if asset else None),
        "file_name": file_name,
        "byte_size": size if not is_api else meta.get("byte_size"),
        "sha256": sha,
        "raw_path": raw_path,
        "archive_path": dest,
        "licence_id": lic_id,
        "licence_evidence": lic_evidence,
        "licence_verified": bool(verified),
        "attribution": src.get("attribution"),
        "status": manifest.CURRENT if verified else manifest.QUARANTINED,
        "status_reason": None if verified else "licence not verified for this asset version",
        "first_seen_run_id": run_id,
        "last_checked_run_id": run_id,
        "superseded_by_version": None,
        "created_at": utcnow(),
    }


def _reject(run_id, source_id, locator, version, code, reason) -> dict:
    return {
        "run_id": run_id,
        "source_id": source_id,
        "asset_key": "*",
        "asset_version": version,
        "record_locator": locator,
        "reason_code": code,
        "reason": reason,
        "original_value": None,
        "evidence": None,
        "rejected_at": utcnow(),
    }


def _reverify_licences(spark, target: Target, reg: registry.Registry, run_id: str, evidence_seen: list) -> int:
    """Look again at versions held back for licence reasons: the registry may now record an operator
    confirmation, or the licence finder may have improved. Returns the number newly verified."""
    held = store.rows(
        spark,
        f"SELECT source_id, asset_key, asset_version, sha256, archive_path, file_name "
        f"FROM {target.table('ops', 'asset_manifest')} WHERE status = 'quarantined' "
        f"AND status_reason = 'licence not verified for this asset version'",
    )
    done = {(e["source_id"], e["asset_key"], e["asset_version"]) for e in evidence_seen}
    verified = 0
    for h in held:
        if (h["source_id"], h["asset_key"], h["asset_version"]) in done:
            continue
        src = reg.sources.get(h["source_id"])
        asset = next((a for a in (src.assets if src else ()) if a.asset_id == h["asset_key"]), None)
        if src is None or asset is None or src.status == "hold" or asset.status == "hold":
            continue
        if asset.licence_verification == "operator_confirmed":
            lic_id, statement, locator = src.licence_id, (src.get("licence") or {}).get("evidence"), "registry"
        else:
            lic_id, statement, locator = assets.find_licence_statement(h["archive_path"], h["file_name"])
        ok = lic_id is not None and lic_id == src.licence_id
        store.append(
            spark,
            target.table("silver", "licence_evidence"),
            [
                {
                    "source_id": h["source_id"],
                    "asset_key": h["asset_key"],
                    "asset_version": h["asset_version"],
                    "sha256": h["sha256"],
                    "licence_id": lic_id,
                    "statement": statement,
                    "locator": locator,
                    "verified": ok,
                    "run_id": run_id,
                }
            ],
        )
        if ok:
            verified += 1
            evidence = sql_literal(f"{statement} [{locator}]"[:3000])
            spark.sql(
                f"UPDATE {target.table('ops', 'asset_manifest')} SET status = 'current', status_reason = NULL, "
                f"licence_verified = TRUE, licence_id = {sql_literal(lic_id)}, licence_evidence = {evidence}, "
                f"last_checked_run_id = '{run_id}' WHERE source_id = '{h['source_id']}' "
                f"AND asset_key = '{h['asset_key']}' AND asset_version = {h['asset_version']}"
            )
    return verified


def _mark_withdrawn(spark, target: Target, reg: registry.Registry) -> int:
    """Assets no longer registered (or put on hold) stay in the manifest as withdrawn."""
    keep = {(i.item_id) for i in reg.stream_items if i.status == "enabled"}
    keys = {("STREAM_APR", k) for k in keep}
    for s in reg.sources.values():
        for a in s.assets:
            if s.status != "hold" and a.status != "hold":
                keys.add((s.source_id, a.asset_id))
    current = store.rows(
        spark,
        f"SELECT DISTINCT source_id, asset_key FROM {target.table('ops', 'asset_manifest')} "
        f"WHERE status = 'current' AND retrieval_mode <> 'manual_bootstrap'",
    )
    gone = [(r["source_id"], r["asset_key"]) for r in current if (r["source_id"], r["asset_key"]) not in keys]
    for sid, key in gone:
        spark.sql(
            f"UPDATE {target.table('ops', 'asset_manifest')} SET status = 'withdrawn', "
            f"status_reason = 'no longer registered or on hold' "
            f"WHERE source_id = '{sid}' AND asset_key = '{key}' AND status = 'current'"
        )
    return len(gone)


# --- parse -------------------------------------------------------------------------------------

SILVER_FOR = {
    "STREAM_APR": "stream_apr_record",
    "COMPANY_APR": "company_apr_record",
    "OFWAT_WCPR": "ofwat_wcpr_record",
    "OFWAT_PR19_ODI": "ofwat_pr19_pc",
    "OFWAT_PR24_OUTCOMES": "ofwat_pr24_pc",
    "RAG_DEFINITIONS": "rag_guideline",
}


def _parsable(spark, target: Target, source_id: str) -> list[dict]:
    return store.rows(
        spark,
        f"""
        SELECT m.*, p.status AS parsed_status, p.parser_version AS parsed_with
        FROM {target.table('ops', 'asset_manifest')} m
        LEFT JOIN (SELECT asset_key, asset_version, status, parser_version,
                          row_number() OVER (PARTITION BY asset_key, asset_version ORDER BY parsed_at DESC) rn
                   FROM {target.table('ops', 'parse_log')} WHERE source_id = '{source_id}') p
          ON p.asset_key = m.asset_key AND p.asset_version = m.asset_version AND p.rn = 1
        WHERE m.source_id = '{source_id}' AND m.status = 'current' AND m.licence_verified""",
    )


def parse(spark, target: Target, run_id: str, source_id: str) -> dict:
    """Parse every current, licence-verified asset version of one source that this parser
    version has not parsed yet. A contract failure quarantines that version only."""
    import pandas as pd

    from . import parsers, parsers_apr, parsers_outcomes

    started = utcnow()
    reg = registry.load_registry()
    companies = registry.load_companies()
    items = {i.item_id: i for i in reg.stream_items}
    silver = target.table("silver", SILVER_FOR[source_id])
    counts = {"parsed": 0, "skipped": 0, "quarantined": 0, "failed": 0, "rows": 0, "rejects": 0}
    failures = []
    for asset in _parsable(spark, target, source_id):
        version = f"{parsers.PARSER_VERSION}"
        if asset["parsed_status"] == "parsed" and (asset["parsed_with"] or "").endswith(version):
            counts["skipped"] += 1
            continue
        path = asset["archive_path"]
        log = {
            "run_id": run_id,
            "source_id": source_id,
            "asset_key": asset["asset_key"],
            "asset_version": asset["asset_version"],
            "contract": reg.source(source_id)["contract"],
            "parser_version": f"{reg.source(source_id)['contract']}@{version}",
            "parsed_at": utcnow(),
        }
        try:
            if source_id == "STREAM_APR":
                meta = json.loads(asset["http_metadata"] or "{}")
                result = parsers.parse_stream(path, asset, items[asset["asset_key"]], meta.get("rag_version"))
            elif source_id == "COMPANY_APR":
                registered = next(a for a in reg.source(source_id).assets if a.asset_id == asset["asset_key"])
                result = parsers_apr.parse_company_apr(path, asset, registered, companies)
            elif source_id == "OFWAT_WCPR":
                result = parsers.parse_wcpr(path, asset, companies)
            elif source_id == "OFWAT_PR19_ODI":
                result = parsers_outcomes.parse_pr19(path, asset, companies)
            elif source_id == "OFWAT_PR24_OUTCOMES":
                result = parsers_outcomes.parse_pr24(path, asset, companies)
            elif source_id == "RAG_DEFINITIONS":
                result = _rag_document(target, asset)
            else:
                raise StageFailed(f"no parser for {source_id}")
        except ContractError as e:
            counts["quarantined"] += 1
            store.append(
                spark,
                target.table("ops", "reject"),
                [
                    {
                        "run_id": run_id,
                        "source_id": source_id,
                        "asset_key": asset["asset_key"],
                        "asset_version": asset["asset_version"],
                        "record_locator": asset["file_name"],
                        "reason_code": "CONTRACT_MISMATCH",
                        "reason": str(e),
                        "original_value": None,
                        "evidence": getattr(e, "evidence", {}),
                        "rejected_at": utcnow(),
                    }
                ],
            )
            store.append(
                spark,
                target.table("ops", "parse_log"),
                [{**log, "status": "quarantined", "rows_out": 0, "rows_rejected": 0, "message": str(e)[:4000]}],
            )
            continue
        except Exception as e:  # noqa: BLE001 - recorded, then the stage fails after independent assets finish
            counts["failed"] += 1
            failures.append(f"{asset['asset_key']}: {type(e).__name__}: {e}")
            store.append(
                spark,
                target.table("ops", "parse_log"),
                [{**log, "status": "failed", "rows_out": 0, "rows_rejected": 0, "message": str(e)[:4000]}],
            )
            continue
        for r in result.rows:
            r["run_id"] = run_id
        spark.sql(
            f"DELETE FROM {silver} WHERE asset_key = '{asset['asset_key']}' "
            f"AND asset_version = {asset['asset_version']}"
        )
        store.append_pandas(spark, silver, pd.DataFrame(result.rows))
        if source_id == "OFWAT_PR19_ODI":
            controls = result.evidence.pop("price_control", [])
            for r in controls:
                r["run_id"] = run_id
            pc = target.table("silver", "ofwat_pr19_price_control_odi")
            spark.sql(
                f"DELETE FROM {pc} WHERE asset_key = '{asset['asset_key']}' "
                f"AND asset_version = {asset['asset_version']}"
            )
            store.append_pandas(spark, pc, pd.DataFrame(controls))
        store.append(
            spark,
            target.table("ops", "reject"),
            [{**r, "run_id": run_id, "rejected_at": utcnow()} for r in result.rejects],
        )
        store.append(
            spark,
            target.table("ops", "parse_log"),
            [
                {
                    **log,
                    "status": "parsed",
                    "rows_out": len(result.rows),
                    "rows_rejected": len(result.rejects),
                    "message": json.dumps(result.evidence, default=str)[:4000],
                }
            ],
        )
        counts["parsed"] += 1
        counts["rows"] += len(result.rows)
        counts["rejects"] += len(result.rejects)
    status = "failed" if failures else "succeeded"
    store.record_stage(spark, target, run_id, "parse", status, started, source_id, "; ".join(failures)[:4000], counts)
    if failures:
        raise StageFailed(f"{source_id}: {len(failures)} asset(s) failed to parse: {failures[:3]}")
    return counts


def _rag_document(target: Target, asset: dict):
    """Record the guideline version and copy the licence-verified PDF to the curated documents Volume."""
    from .parsers import ParseResult, _provenance

    text, pages = assets.pdf_text(asset["archive_path"], max_pages=2)
    m = re.search(r"RAG\s*4\.(\d{1,2})", text) or re.search(r"RAG[- ]4\.(\d{1,2})", asset["file_name"])
    version = f"4.{m.group(1)}" if m else None
    dest = f"{target.documents_root}/ofwat/rag/{manifest.safe_name(asset['file_name'])}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.copyfile(asset["archive_path"], dest)
    row = {
        **_provenance(asset, f"file={asset['file_name']}", "rag_guideline_document_v1"),
        "rag_version": version,
        "published": asset.get("published"),
        "document_path": dest,
        "page_count": pages,
        "licence_statement": asset.get("licence_evidence"),
    }
    return ParseResult(rows=[row], evidence={"rag_version": version, "pages": pages, "document_path": dest})


# --- publish (build the Gold candidate release) ------------------------------------------------


def _config_views(spark, target: Target) -> None:
    """Temporary views over the governed configuration, used by the release SQL."""
    from . import gold

    reg = registry.load_registry()
    companies = registry.load_companies()
    rows = gold.config_rows(target.company_code)
    for name in ("dim_company", "dim_period", "dim_source", "dim_metric", "metric_source_map"):
        schema = gold.RELEASE_STORES[name]
        _temp_view(spark, f"cfg_{name}", rows[name], schema)
    order = []
    for s in reg.sources.values():
        for a in s.assets:
            order.append(
                {
                    "source_id": s.source_id,
                    "asset_key": a.asset_id,
                    "release_rank": (a.report_year_to or 0) * 10 + (1 if a.supersedes else 0),
                }
            )
    _temp_view(spark, "cfg_asset_order", order, "source_id STRING, asset_key STRING, release_rank INT")
    items = [
        {
            "item_id": i.item_id,
            "company_code": i.company_code,
            "financial_year": i.financial_year,
            "company_name": companies[i.company_code].company_name,
        }
        for i in reg.stream_items
    ]
    _temp_view(
        spark,
        "cfg_stream_item",
        items,
        "item_id STRING, company_code STRING, financial_year STRING, " "company_name STRING",
    )


def _temp_view(spark, name: str, rows: list[dict], ddl: str) -> None:
    import datetime as dt

    from .gold import _split_cols

    cols = [c.split()[:2] for c in _split_cols(ddl.replace(" NOT NULL", ""))]

    def conv(v, typ):
        if v is not None and typ.upper() == "DATE" and isinstance(v, str):
            return dt.date.fromisoformat(v)
        if v is not None and typ.upper() == "DOUBLE":
            return float(v)
        return v

    data = [tuple(conv(r.get(n), t) for n, t in cols) for r in rows]
    spark.createDataFrame(data, ", ".join(f"{n} {t}" for n, t in cols)).createOrReplaceTempView(name)


def _fmt(sql: str, target: Target, **extra) -> str:
    return sql.format(ops=target.schema("ops"), silver=target.schema("silver"), gold=target.schema("gold"), **extra)


def ensure_gold_views(spark, target: Target) -> None:
    """(Re)create every Gold view. Definitions only: no data moves, so this is safe on every run."""
    from . import gold, gold_build

    comments = {
        "dim_company": "England and Wales water companies (governed crosswalk). service_type: water_and_sewerage or water_only.",
        "dim_period": "Financial years. report_year is the year the financial year ends (2024-25 = 2025). AMP7 = PR19, AMP8 = PR24.",
        "dim_source": "Public sources with publisher, licence, attribution and status (enabled, conditional or hold).",
        "dim_metric": "Canonical metrics: definition, unit, price base, direction and whether companies may be compared.",
        "metric_source_map": "Explicit crosswalk from source items to canonical metrics.",
        "deployment_context": "Exactly one row: the company this deployment answers for when users say we, us or our.",
        "fact_reported_performance": "Reported values (Ofwat WCPR and Stream APR) with provenance and comparability.",
        "fact_performance_commitment": "Performance commitment levels and actuals (PR19 and PR24 kept separate).",
        "fact_odi": "PR19 outcome delivery incentive payments, GBP million in 2017-18 prices.",
        "reconciliation": "Values reported by both Ofwat WCPR and Stream for the same metric, company and year.",
        "source_attribution": "Attribution notice for every source asset version in the published release.",
    }
    for name in gold.RELEASE_STORES:
        spark.sql(_fmt(gold.projection_view(name, comments[name]), target))
    for name, (body, comment) in gold_build.CURATED_VIEWS.items():
        spark.sql(
            f"CREATE OR REPLACE VIEW {target.schema('gold')}.{name} COMMENT {sql_literal(comment)} AS "
            + _fmt(body, target)
        )
    spark.sql(_fmt(gold_build.METRIC_VIEW, target))


def conform(spark, target: Target, run_id: str) -> dict:
    """Conform the source-specific Silver tables into one observation table with canonical company,
    reporting area and financial year, using only current, licence-verified asset versions."""
    from . import gold_build

    started = utcnow()
    _config_views(spark, target)
    current = _fmt(gold_build.CURRENT_ASSETS, target)
    spark.sql(_fmt(gold_build.CONFORM_SQL, target, current=current))
    counts = {
        r["source_id"]: r["n"]
        for r in store.rows(
            spark,
            f"SELECT source_id, count(*) n FROM "
            f"{target.table('silver', 'conformed_observation')} GROUP BY source_id",
        )
    }
    store.record_stage(spark, target, run_id, "conform", "succeeded", started, None, json.dumps(counts), counts)
    return counts


def publish(spark, target: Target, run_id: str) -> dict:
    """Write a candidate release (invisible to Gold views until finalize) and refresh view definitions."""
    from . import gold, gold_build

    started = utcnow()
    migrations.apply_migrations(spark, target, run_id)
    _config_views(spark, target)
    release = run_id
    for name in gold.RELEASE_STORES:
        spark.sql(f"DELETE FROM {target.table('silver', 'release_' + name)} WHERE release_id = '{release}'")
    rows = gold.config_rows(target.company_code)
    for name in ("dim_company", "dim_period", "dim_source", "dim_metric", "metric_source_map", "deployment_context"):
        store.append(
            spark, target.table("silver", "release_" + name), [{"release_id": release, **r} for r in rows[name]]
        )
    current = _fmt(gold_build.CURRENT_ASSETS, target)
    base = _fmt(gold_build.REPORTED_BASE, target)
    unit_check = f"{gold_build.norm_unit('source_unit')} <> {gold_build.norm_unit('rule_unit')}"
    statements = {
        "fact_reported_performance": _fmt(
            gold_build.REPORTED_INSERT, target, base=base, release=release, unit_check=unit_check
        ),
        "fact_performance_commitment": _fmt(gold_build.COMMITMENT_INSERT, target, current=current, release=release),
        "fact_odi": _fmt(gold_build.ODI_INSERT, target, current=current, release=release),
        "reconciliation": _fmt(gold_build.RECONCILIATION_INSERT, target, release=release),
        "source_attribution": _fmt(gold_build.ATTRIBUTION_INSERT, target, release=release),
    }
    counts = {}
    for name, sql in statements.items():
        spark.sql(sql)
        counts[name] = spark.sql(
            f"SELECT count(*) n FROM {target.table('silver', 'release_' + name)} " f"WHERE release_id = '{release}'"
        ).collect()[0]["n"]
    spark.sql(f"DELETE FROM {target.table('ops', 'release')} WHERE release_id = '{release}'")  # a retried task
    store.append(
        spark,
        target.table("ops", "release"),
        [
            {
                "release_id": release,
                "run_id": run_id,
                "status": "candidate",
                "created_at": utcnow(),
                "published_at": None,
                "summary": counts,
            }
        ],
    )
    ensure_gold_views(spark, target)
    store.record_stage(
        spark, target, run_id, "publish", "succeeded", started, None, f"candidate release {release} built", counts
    )
    return counts


# --- quality gates -----------------------------------------------------------------------------


def _quality_config() -> dict:
    import yaml

    return yaml.safe_load((registry.CONFIG_DIR / "quality.yml").read_text(encoding="utf-8"))


def quality_checks(spark, target: Target, release: str) -> list[dict]:
    """Evaluate the blocking and warning checks for one candidate release. Pure SQL over the stores."""
    cfg = _quality_config()
    s = lambda name: target.table("silver", "release_" + name)  # noqa: E731
    r = f"release_id = '{release}'"
    one = lambda q: spark.sql(q).collect()[0][0]  # noqa: E731
    results = []

    def check(check_id, severity, scope, observed, expected, passed, message):
        results.append(
            {
                "check_id": check_id,
                "severity": severity,
                "scope": scope,
                "observed": str(observed),
                "expected": str(expected),
                "status": "pass" if passed else "fail",
                "message": message,
            }
        )

    n = one(f"SELECT count(*) FROM {s('deployment_context')} WHERE {r}")
    known = one(
        f"SELECT count(*) FROM {s('deployment_context')} d JOIN {s('dim_company')} c "
        f"ON c.company_code = d.company_code AND c.release_id = d.release_id WHERE d.{r}"
    )
    check(
        "deployment_context_single_known_company",
        "blocking",
        "deployment_context",
        f"{n} rows, {known} known",
        "1 row, 1 known",
        n == 1 and known == 1,
        "Exactly one configured company that exists in dim_company.",
    )
    for name, key in (
        ("fact_reported_performance", "observation_id"),
        ("fact_performance_commitment", "commitment_id"),
        ("fact_odi", "odi_id"),
        ("dim_metric", "metric_id"),
        ("dim_company", "company_code"),
    ):
        dupes = one(f"SELECT count(*) FROM (SELECT {key} FROM {s(name)} WHERE {r} GROUP BY {key} HAVING count(*) > 1)")
        check(f"grain_{name}", "blocking", name, dupes, 0, dupes == 0, f"{key} is unique.")
    for name in ("fact_reported_performance", "fact_performance_commitment", "fact_odi"):
        orphan = one(f"""SELECT count(*) FROM {s(name)} f LEFT JOIN {target.table('ops', 'asset_manifest')} m
                         ON m.source_id = f.source_id AND m.asset_key = f.asset_key AND m.asset_version = f.asset_version
                         WHERE f.{r} AND (m.sha256 IS NULL OR NOT m.licence_verified OR m.licence_id IS NULL
                               OR f.record_locator IS NULL OR f.sha256 <> m.sha256)""")
        check(
            f"provenance_{name}",
            "blocking",
            name,
            orphan,
            0,
            orphan == 0,
            "Every row joins a licence-verified manifest version with matching checksum and a record locator.",
        )
    bad = one(f"""SELECT count(*) FROM {s('fact_reported_performance')} f LEFT JOIN {s('dim_metric')} m
                  ON m.metric_id = f.metric_id AND m.release_id = f.release_id
                  WHERE f.{r} AND f.is_comparable AND (m.metric_id IS NULL OR m.comparability_status <> 'comparable'
                        OR f.unit IS NULL OR f.price_base IS NULL OR f.reporting_area <> 'whole_company')""")
    check(
        "comparability_integrity",
        "blocking",
        "fact_reported_performance",
        bad,
        0,
        bad == 0,
        "Comparable rows have an approved metric, unit, price base and whole-company scope.",
    )
    multi = one(f"""SELECT count(*) FROM (SELECT metric_id, company_code, report_year FROM {s('fact_reported_performance')}
                    WHERE {r} AND is_comparable AND is_preferred_source GROUP BY 1, 2, 3 HAVING count(*) > 1)""")
    check(
        "one_preferred_value",
        "blocking",
        "fact_reported_performance",
        multi,
        0,
        multi == 0,
        "At most one preferred comparable value per metric, company and year.",
    )
    wrong_amp = one(f"""SELECT count(*) FROM {s('fact_performance_commitment')} WHERE {r} AND
                        ((price_review = 'PR19' AND report_year NOT BETWEEN 2021 AND 2025)
                         OR (price_review = 'PR24' AND report_year NOT BETWEEN 2026 AND 2030))""")
    check(
        "price_review_periods",
        "blocking",
        "fact_performance_commitment",
        wrong_amp,
        0,
        wrong_amp == 0,
        "PR19 rows sit in AMP7 years and PR24 rows in AMP8 years.",
    )
    foreign = one(f"SELECT count(*) FROM {s('dim_company')} WHERE {r} AND jurisdiction <> 'england_wales'")
    check(
        "jurisdiction_scope",
        "blocking",
        "dim_company",
        foreign,
        0,
        foreign == 0,
        "Only England and Wales companies are modelled in this release.",
    )

    current_fy = periods.parse_financial_year(_current_fy_label()).report_year
    for sid, conf in (cfg.get("completeness") or {}).items():
        fact = (
            "fact_performance_commitment"
            if sid in ("OFWAT_PR19_ODI", "OFWAT_PR24_OUTCOMES")
            else "fact_reported_performance"
        )
        rows_n = one(f"SELECT count(*) FROM {s(fact)} WHERE {r} AND source_id = '{sid}'")
        src = registry.load_registry().source(sid)
        guaranteed = src.status == "enabled"
        if rows_n == 0 and not guaranteed:
            check(
                f"completeness_{sid}",
                "warning",
                sid,
                0,
                "rows when licence-verified",
                False,
                f"{sid} is conditional and has no licence-verified asset in this release; it is not available.",
            )
            continue
        latest = one(f"SELECT max(report_year) FROM {s(fact)} WHERE {r} AND source_id = '{sid}'")
        companies = (
            one(
                f"SELECT count(DISTINCT company_code) FROM {s(fact)} WHERE {r} AND source_id = '{sid}' "
                f"AND report_year = {latest or 0}"
            )
            if fact == "fact_reported_performance"
            else one(f"SELECT count(DISTINCT company_code) FROM {s(fact)} WHERE {r} AND source_id = '{sid}'")
        )
        need_c = conf.get("min_companies_latest_year") or conf.get("min_companies") or 0
        need_r = conf.get("min_rows", 0)
        check(
            f"completeness_{sid}",
            "blocking" if guaranteed else "warning",
            sid,
            f"{rows_n} rows, {companies} companies (latest year {latest})",
            f">= {need_r} rows, >= {need_c} companies",
            rows_n >= need_r and companies >= need_c,
            f"Completeness threshold for {sid}.",
        )
        lag = (cfg.get("freshness") or {}).get(f"{sid}_max_report_year_lag")
        if lag is not None:
            check(
                f"freshness_{sid}",
                "blocking",
                sid,
                latest,
                f">= {current_fy - lag}",
                latest is not None and latest >= current_fy - lag,
                f"Latest report year is no more than {lag} years behind the current financial year.",
            )
    disagree = one(f"SELECT count(*) FROM {s('reconciliation')} WHERE {r} AND status = 'disagree'")
    limit = (cfg.get("reconciliation") or {}).get("max_disagreements_warning", 0)
    check(
        "reconciliation_wcpr_stream",
        "warning",
        "reconciliation",
        disagree,
        f"<= {limit}",
        disagree <= limit,
        "WCPR and Stream values for the same metric, company and year agree within tolerance.",
    )
    attributions = one(f"SELECT count(*) FROM {s('source_attribution')} WHERE {r}")
    used = one(
        f"SELECT count(DISTINCT source_id, asset_key, asset_version) FROM {s('fact_reported_performance')} WHERE {r}"
    )
    check(
        "attribution_generated",
        "blocking",
        "source_attribution",
        attributions,
        f">= {used}",
        attributions >= used,
        "An attribution row exists for every source asset version used.",
    )
    return results


def _current_fy_label(today=None) -> str:
    import datetime as dt

    d = today or dt.date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def quality(spark, target: Target, run_id: str) -> dict:
    started = utcnow()
    release = run_id
    exists = spark.sql(
        f"SELECT count(*) n FROM {target.table('ops', 'release')} WHERE release_id = '{release}'"
    ).collect()[0]["n"]
    if not exists:
        store.record_stage(spark, target, run_id, "quality", "failed", started, None, "no candidate release to check")
        raise StageFailed("publish did not produce a candidate release")
    results = quality_checks(spark, target, release)
    store.append(
        spark,
        target.table("ops", "quality_result"),
        [{"run_id": run_id, "release_id": release, **x, "checked_at": utcnow()} for x in results],
    )
    failed = [x["check_id"] for x in results if x["status"] == "fail" and x["severity"] == "blocking"]
    warned = [x["check_id"] for x in results if x["status"] == "fail" and x["severity"] == "warning"]
    store.record_stage(
        spark,
        target,
        run_id,
        "quality",
        "failed" if failed else "succeeded",
        started,
        None,
        f"blocking failures: {failed}; warnings: {warned}",
        {"checks": len(results), "blocking_failures": failed, "warnings": warned},
    )
    return {"checks": len(results), "blocking_failures": failed, "warnings": warned}


# --- finalize and rollback ---------------------------------------------------------------------


def _switch(spark, target: Target, release: str, by: str) -> str | None:
    """Point every Gold view at `release` in one Delta commit. Returns the previous release ID."""
    prev = store.rows(spark, f"SELECT release_id FROM {target.table('ops', 'published_release')}")
    previous = prev[0]["release_id"] if prev else None
    spark.sql(
        f"INSERT OVERWRITE {target.table('ops', 'published_release')} "
        f"SELECT '{release}', current_timestamp(), {sql_literal(by)}, {sql_literal(previous)}"
    )
    return previous


def finalize(spark, target: Target, run_id: str) -> dict:
    """Publish the candidate if every blocking gate passed, then fail the run if any stage failed."""
    started = utcnow()
    release = run_id
    cfg = _quality_config()
    stages = store.rows(
        spark,
        f"SELECT stage, source_id, status, message FROM {target.table('ops', 'pipeline_run')} "
        f"WHERE run_id = '{run_id}'",
    )
    gates = store.rows(
        spark,
        f"SELECT check_id, severity, status FROM {target.table('ops', 'quality_result')} "
        f"WHERE release_id = '{release}'",
    )
    blocking = [g["check_id"] for g in gates if g["severity"] == "blocking" and g["status"] == "fail"]
    candidate = any(s["stage"] == "publish" and s["status"] == "succeeded" for s in stages)
    published = False
    if candidate and gates and not blocking:
        previous = _switch(spark, target, release, f"refresh_performance run {run_id}")
        rel = target.table("ops", "release")
        spark.sql(f"UPDATE {rel} SET status = 'superseded' WHERE status = 'published' AND release_id <> '{release}'")
        spark.sql(
            f"UPDATE {rel} SET status = 'published', published_at = current_timestamp() WHERE release_id = '{release}'"
        )
        published = True
        _apply_retention(spark, target, (cfg.get("retention") or {}).get("keep_published_releases", 5))
    else:
        # Not published: a candidate (if publish got that far) is rejected; the previous release stays live.
        spark.sql(f"UPDATE {target.table('ops', 'release')} SET status = 'rejected' WHERE release_id = '{release}'")
        previous = None
    failed_stages = [f"{s['stage']}/{s['source_id'] or '-'}" for s in stages if s["status"] == "failed"]
    message = (
        f"published release {release} (previous {previous})"
        if published
        else f"release {release} NOT published; blocking gates {blocking}; the previous release stays live"
    )
    status = "succeeded" if published and not failed_stages else "failed"
    store.record_stage(
        spark,
        target,
        run_id,
        "finalize",
        status,
        started,
        None,
        f"{message}; failed stages: {failed_stages}",
        {"published": published},
    )
    if status == "failed":
        raise StageFailed(f"{message}; failed stages: {failed_stages}")
    return {"published": release, "previous": previous}


def _apply_retention(spark, target: Target, keep: int) -> None:
    from . import gold

    keep_ids = [
        r["release_id"]
        for r in store.rows(
            spark,
            f"""
        SELECT release_id FROM {target.table('ops', 'release')} WHERE status IN ('published', 'superseded')
        ORDER BY published_at DESC NULLS LAST LIMIT {int(keep)}""",
        )
    ]
    if not keep_ids:
        return
    keep_sql = ", ".join(sql_literal(k) for k in keep_ids)
    for name in gold.RELEASE_STORES:
        spark.sql(f"DELETE FROM {target.table('silver', 'release_' + name)} WHERE release_id NOT IN ({keep_sql})")


def rollback(spark, target: Target, run_id: str, release_id: str | None = None) -> dict:
    """Point Gold at an earlier release that is still in the release stores."""
    started = utcnow()
    if not release_id:
        raise StageFailed(
            "rollback needs --release-id; list candidates in ops.release (status published or superseded)"
        )
    ok = spark.sql(
        f"SELECT count(*) n FROM {target.table('silver', 'release_deployment_context')} "
        f"WHERE release_id = {sql_literal(release_id)}"
    ).collect()[0]["n"]
    if not ok:
        raise StageFailed(f"release {release_id} is not in the release stores (it may be past retention)")
    previous = _switch(spark, target, release_id, f"rollback run {run_id}")
    rel = target.table("ops", "release")
    spark.sql(f"UPDATE {rel} SET status = 'rolled_back' WHERE release_id = {sql_literal(previous)}")
    spark.sql(f"UPDATE {rel} SET status = 'published' WHERE release_id = {sql_literal(release_id)}")
    store.record_stage(
        spark,
        target,
        run_id,
        "rollback",
        "succeeded",
        started,
        None,
        f"published release {release_id}; previous {previous}",
    )
    return {"published": release_id, "previous": previous}
