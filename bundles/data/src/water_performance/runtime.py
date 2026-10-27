"""Job runtime: resolve the deployment target from task arguments and read/write tables.

Every location comes from the arguments that the bundle passes to each task. Nothing here
knows a workspace, catalog, schema or Volume literal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, fields, replace

LOG = logging.getLogger("water_performance")
_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def check_identifier(name: str) -> str:
    if not name or not _IDENT.match(name):
        raise ValueError(f"invalid Unity Catalog identifier {name!r}: use letters, digits and underscores")
    return name


@dataclass(frozen=True)
class Target:
    """Where one deployment keeps its data. Built from task arguments."""

    catalog: str
    landing_schema: str
    ops_schema: str
    silver_schema: str
    gold_schema: str
    test_schema: str
    raw_volume: str
    archive_volume: str
    documents_volume: str
    reports_volume: str
    company_code: str
    # Integration tests only: a folder that replaces the raw, archive and documents Volumes.
    volume_root_override: str | None = None

    def __post_init__(self) -> None:
        for f in fields(self):
            if f.name not in ("company_code", "volume_root_override"):
                check_identifier(getattr(self, f.name))

    def schema_name(self, layer: str) -> str:
        return {
            "landing": self.landing_schema,
            "ops": self.ops_schema,
            "silver": self.silver_schema,
            "gold": self.gold_schema,
            "test": self.test_schema,
        }[layer]

    def schema(self, layer: str) -> str:
        return f"`{self.catalog}`.`{self.schema_name(layer)}`"

    def table(self, layer: str, name: str) -> str:
        return f"{self.schema(layer)}.`{check_identifier(name)}`"

    def volume(self, name: str, *parts: str) -> str:
        schema = self.ops_schema if name == self.reports_volume else self.landing_schema
        if name not in (self.raw_volume, self.archive_volume, self.documents_volume, self.reports_volume):
            raise ValueError(f"{name!r} is not one of this deployment's Volumes")
        base = f"/Volumes/{self.catalog}/{schema}/{name}"
        return "/".join([base, *[str(p).strip("/") for p in parts if p]])

    def _root(self, volume: str, name: str) -> str:
        return f"{self.volume_root_override}/{name}" if self.volume_root_override else self.volume(volume)

    @property
    def raw_root(self) -> str:
        return self._root(self.raw_volume, "raw")

    @property
    def archive_root(self) -> str:
        return self._root(self.archive_volume, "archive")

    @property
    def documents_root(self) -> str:
        return self._root(self.documents_volume, "curated_documents")

    def for_test(self, volume_root: str) -> Target:
        """Ops, Silver and Gold all in the isolated test schema; Volumes replaced by a test folder."""
        return replace(
            self,
            ops_schema=self.test_schema,
            silver_schema=self.test_schema,
            gold_schema=self.test_schema,
            volume_root_override=volume_root,
        )


TARGET_ARGS = tuple(f.name for f in fields(Target) if f.name != "volume_root_override")


def add_target_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--target", required=True, help=f"JSON object with the keys {', '.join(TARGET_ARGS)}")
    parser.add_argument("--run-id", dest="run_id", required=True, help="the job run ID")
    return parser


def target_from_json(text: str) -> Target:
    try:
        values = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"--target is not valid JSON: {e}") from None
    missing = [k for k in TARGET_ARGS if not values.get(k)]
    unknown = sorted(set(values) - set(TARGET_ARGS))
    unresolved = [k for k, v in values.items() if "${" in str(v)]
    if missing or unknown or unresolved:
        raise ValueError(f"--target is incomplete: missing={missing} unknown={unknown} unresolved={unresolved}")
    return Target(**{k: values[k] for k in TARGET_ARGS})


def target_from_args(args: argparse.Namespace) -> Target:
    return target_from_json(args.target)


def get_spark():
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def to_json(value) -> str:
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
