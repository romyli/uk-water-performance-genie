"""Content identity and versioning for source assets.

Every retrieved file or API snapshot gets a SHA-256. A manifest entry is keyed by
(source_id, asset_key). Unchanged content is skipped, changed content becomes the next version,
and older versions stay visible as superseded. Nothing is deleted.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

NEW, UNCHANGED, CHANGED = "new", "unchanged", "changed"
CURRENT, SUPERSEDED, WITHDRAWN, QUARANTINED = "current", "superseded", "withdrawn", "quarantined"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk: int = 1 << 20) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def canonical_records(records: Iterable[dict], key: str) -> bytes:
    """Deterministic JSON Lines for an API snapshot: one record per line, ordered by `key`,
    keys sorted. The same records always produce the same bytes, whatever the page order."""
    rows = sorted(records, key=lambda r: r[key])
    buf = io.StringIO()
    for r in rows:
        buf.write(json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        buf.write("\n")
    return buf.getvalue().encode("utf-8")


def gzip_deterministic(data: bytes) -> bytes:
    """Gzip with a fixed timestamp so identical content gives identical archive bytes."""
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=6) as f:
        f.write(data)
    return out.getvalue()


def safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._")
    return cleaned[:150] or "asset"


@dataclass(frozen=True)
class Decision:
    action: str  # new | unchanged | changed
    version: int
    previous_version: int | None


def decide(sha256: str, existing: list[dict]) -> Decision:
    """Compare content with every recorded version of one asset.

    `existing` holds manifest rows with `asset_version`, `sha256` and `status`. A checksum seen
    before (even on a superseded version) is not ingested again: a publisher reverting to old
    content is recorded as unchanged against that version, not as a new release."""
    if not existing:
        return Decision(NEW, 1, None)
    for row in existing:
        if row["sha256"] == sha256:
            return Decision(UNCHANGED, int(row["asset_version"]), int(row["asset_version"]))
    latest = max(int(r["asset_version"]) for r in existing)
    return Decision(CHANGED, latest + 1, latest)


def archive_relpath(source_id: str, asset_key: str, sha256: str, file_name: str) -> str:
    """Content-addressed archive path. The checksum in the path makes each copy immutable."""
    return f"{safe_name(source_id)}/{safe_name(asset_key)}/sha256={sha256}/{safe_name(file_name)}"


# --- Manual bootstrap ------------------------------------------------------------------------

BOOTSTRAP_SUFFIX = ".bootstrap.json"
BOOTSTRAP_REQUIRED = (
    "source_id",
    "asset_id",
    "landing_page",
    "asset_url",
    "release",
    "published",
    "licence_id",
    "licence_evidence",
    "supplied_by",
    "downloaded_at",
    "sha256",
)


class BootstrapError(ValueError):
    """A manually supplied asset lacks the metadata needed to trace it."""


def validate_bootstrap(meta: dict, actual_sha256: str, known_licences: Iterable[str]) -> dict:
    """Check an operator-supplied sidecar. The file's checksum must match what the operator
    recorded when they downloaded it, and the public pages must be https URLs."""
    missing = [k for k in BOOTSTRAP_REQUIRED if not meta.get(k)]
    if missing:
        raise BootstrapError(f"bootstrap metadata is missing {missing}")
    for key in ("landing_page", "asset_url"):
        if not str(meta[key]).startswith("https://"):
            raise BootstrapError(f"{key} must be the public https URL, got {meta[key]!r}")
    if meta["licence_id"] not in set(known_licences):
        raise BootstrapError(f"licence_id {meta['licence_id']!r} is not a recognised open licence")
    if meta["sha256"].lower() != actual_sha256.lower():
        raise BootstrapError(
            f"checksum mismatch: the operator recorded {meta['sha256']} but the uploaded file is {actual_sha256}"
        )
    try:
        dt.datetime.fromisoformat(str(meta["downloaded_at"]).replace("Z", "+00:00"))
    except ValueError:
        raise BootstrapError(f"downloaded_at {meta['downloaded_at']!r} is not an ISO 8601 timestamp") from None
    return meta
