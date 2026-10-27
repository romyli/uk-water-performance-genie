#!/usr/bin/env python3
"""Upload a manually downloaded public source asset to the raw Volume (manual bootstrap).

Use this only when a publisher blocks automated retrieval. Download the asset yourself from the
publisher's page, then run from the repository root:

    python3 tools/source_upload.py --target prod --profile <profile> --var catalog=<catalog> \\
      --source-id OFWAT_PR24_OUTCOMES --asset-id pr24_key_dataset_1_outcomes_v9 \\
      --file ~/Downloads/PR24-Key-Dataset-1-Outcomes-data-V9.xlsx \\
      --landing-page https://www.ofwat.gov.uk/... --asset-url https://www.ofwat.gov.uk/wp-content/... \\
      --release "Key Dataset 1 Outcomes data V9" --published 2025-06 \\
      --licence-id OGL-UK-3.0 --licence-evidence "OGL v3.0 statement on the landing page" \\
      --supplied-by "<your name or team>" --dry-run

The file and a `<file>.bootstrap.json` sidecar go to
`/Volumes/<catalog>/<landing schema>/<raw volume>/manual/<source_id>/<asset_id>/`. The next
refresh_performance run checks the sidecar, recomputes the SHA-256, archives the file and
ingests it through the same manifest and contract as an automated retrieval. Existing files are
never overwritten. Uses only the Python standard library and the Databricks CLI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "bundles" / "data"
SOURCES = DATA / "src" / "water_performance" / "config" / "sources.yml"
LICENCES = ("CC-BY-4.0", "OGL-UK-3.0")


def registered_assets() -> dict[str, set[str]]:
    """source_id -> registered asset IDs, read from sources.yml without a YAML library."""
    out: dict[str, set[str]] = {}
    current = None
    for line in SOURCES.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^  - source_id: (\S+)", line):
            current = m.group(1)
            out[current] = set()
        elif current and (m := re.match(r"^\s+- asset_id: (\S+)", line)):
            out[current].add(m.group(1))
    return out


def cli(args: list[str], profile: str | None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["databricks", *args] + (["--profile", profile] if profile else []), cwd=cwd, capture_output=True, text=True
    )


def resolve_raw_volume(target: str, profile: str | None, var_args: list[str]) -> str:
    p = cli(["bundle", "validate", "-t", target, "-o", "json", *var_args], profile, DATA)
    if p.returncode != 0:
        raise SystemExit(f"cannot resolve the data bundle: {(p.stderr or p.stdout).strip()[:800]}")
    res = json.loads(p.stdout)["resources"]
    schemas = {k: v["name"] for k, v in res["schemas"].items()}
    raw = res["volumes"]["raw"]
    schema = raw["schema_name"]
    if m := re.fullmatch(r"\$\{resources\.schemas\.([a-z_]+)\.name\}", schema):
        schema = schemas[m.group(1)]
    return f"/Volumes/{raw['catalog_name']}/{schema}/{raw['name']}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", required=True)
    p.add_argument("--profile")
    p.add_argument("--var", action="append", default=[], help="data bundle variable name=value (repeatable)")
    p.add_argument("--source-id", required=True)
    p.add_argument("--asset-id", required=True)
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--landing-page", required=True, help="publisher page that links to the asset")
    p.add_argument("--asset-url", required=True, help="exact public URL of the downloaded asset")
    p.add_argument("--release", required=True)
    p.add_argument("--published", required=True, help="publication date shown by the publisher (YYYY-MM-DD or YYYY-MM)")
    p.add_argument("--licence-id", required=True, choices=LICENCES)
    p.add_argument("--licence-evidence", required=True, help="where the licence is stated, quoted briefly")
    p.add_argument("--supplied-by", required=True, help="person or team supplying the file")
    p.add_argument("--downloaded-at", default=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
    p.add_argument("--dry-run", action="store_true", help="print what would happen and upload nothing")
    args = p.parse_args(argv)

    assets = registered_assets()
    if args.source_id not in assets:
        raise SystemExit(f"unknown source_id {args.source_id!r}; registered: {', '.join(sorted(assets))}")
    if assets[args.source_id] and args.asset_id not in assets[args.source_id]:
        raise SystemExit(
            f"{args.asset_id!r} is not registered for {args.source_id}; registered: "
            f"{', '.join(sorted(assets[args.source_id]))}. Register the asset in sources.yml first."
        )
    for key in ("landing_page", "asset_url"):
        if not getattr(args, key).startswith("https://"):
            raise SystemExit(f"--{key.replace('_', '-')} must be the public https URL")
    if not args.file.is_file():
        raise SystemExit(f"{args.file} is not a file")
    if args.file.suffix.lower() in (".html", ".htm"):
        raise SystemExit("this looks like a web page, not the data asset; download the asset itself")

    var_args = [a for v in args.var for a in ("--var", v)]
    raw = resolve_raw_volume(args.target, args.profile, var_args)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", args.file.name).strip("._")
    dest_dir = f"{raw}/manual/{args.source_id}/{args.asset_id}"
    dest = f"{dest_dir}/{name}"
    if not dest.startswith(raw + "/manual/"):
        raise SystemExit(f"refusing to write outside {raw}/manual/: {dest}")
    digest = sha256(args.file)
    meta = {
        "source_id": args.source_id,
        "asset_id": args.asset_id,
        "landing_page": args.landing_page,
        "asset_url": args.asset_url,
        "release": args.release,
        "published": args.published,
        "licence_id": args.licence_id,
        "licence_evidence": args.licence_evidence,
        "supplied_by": args.supplied_by,
        "downloaded_at": args.downloaded_at,
        "sha256": digest,
        "byte_size": args.file.stat().st_size,
        "original_file_name": args.file.name,
    }
    print(f"destination   {dest}")
    print(f"sidecar       {dest}.bootstrap.json")
    print(f"sha256        {digest} ({meta['byte_size']} bytes)")
    print(json.dumps(meta, indent=2))
    existing = cli(["fs", "ls", f"dbfs:{dest_dir}", "-o", "json"], args.profile)
    if existing.returncode == 0 and name in existing.stdout:
        raise SystemExit(f"{dest} already exists. Files are never overwritten; upload a new release under a new name.")
    if args.dry_run:
        print("\nDry run: nothing uploaded.")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        side = Path(tmp) / f"{name}.bootstrap.json"
        side.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        for local, remote in ((args.file, dest), (side, f"{dest}.bootstrap.json")):
            r = cli(["fs", "cp", str(local), f"dbfs:{remote}"], args.profile)
            if r.returncode != 0:
                raise SystemExit(f"upload failed for {remote}: {(r.stderr or r.stdout).strip()[:500]}")
    print(
        f"\nUploaded. Run refresh_performance; the inventory_archive task will verify {digest[:12]}... and archive it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
