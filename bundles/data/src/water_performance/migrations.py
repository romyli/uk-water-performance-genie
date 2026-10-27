"""Ordered, idempotent schema migrations for the ops, Silver and Gold layers.

Each migration has a version and a list of SQL statements with `{ops}`, `{silver}` and `{gold}`
placeholders for fully qualified schema names. Applied versions are recorded in
`<ops>.schema_migration`. Statements must be safe to repeat (IF NOT EXISTS, CREATE OR REPLACE
VIEW, guarded ADD COLUMNS), because a failed run may stop part-way through a migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

LOG = logging.getLogger("water_performance.migrations")

PROVENANCE_COLS = """
  source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT NOT NULL, sha256 STRING NOT NULL,
  release_label STRING, record_locator STRING NOT NULL, parser_version STRING NOT NULL, run_id STRING NOT NULL
"""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple  # SQL strings, or AddColumn steps


@dataclass(frozen=True)
class AddColumn:
    """ALTER TABLE ... ADD COLUMNS, skipped when the column already exists (safe to repeat)."""

    layer: str
    table: str
    column: str
    type: str
    comment: str = ""


def _t(layer: str, name: str, cols: str, comment: str) -> str:
    return f"CREATE TABLE IF NOT EXISTS {{{layer}}}.{name} ({cols.strip()}) COMMENT '{comment}'"


OPS_TABLES = (
    _t(
        "ops",
        "pipeline_run",
        """
      run_id STRING NOT NULL, stage STRING NOT NULL, source_id STRING, status STRING NOT NULL, message STRING,
      metrics STRING, started_at TIMESTAMP, finished_at TIMESTAMP""",
        "One row per job stage and source: status, message and metrics.",
    ),
    _t(
        "ops",
        "source_registry_snapshot",
        """
      run_id STRING NOT NULL, source_id STRING NOT NULL, priority STRING, status STRING NOT NULL,
      effective_status STRING NOT NULL, gate_failures STRING, registry_entry STRING, captured_at TIMESTAMP""",
        "The registry entry and acceptance-gate result for every source at the start of each run.",
    ),
    _t(
        "ops",
        "source_candidate",
        """
      run_id STRING NOT NULL, source_id STRING NOT NULL, candidate_id STRING NOT NULL, title STRING,
      licence_text STRING, registered BOOLEAN, discovered_at TIMESTAMP""",
        "Public publisher items found by discovery. Unregistered candidates are never ingested.",
    ),
    _t(
        "ops",
        "retrieval_event",
        """
      run_id STRING NOT NULL, source_id STRING NOT NULL, asset_key STRING NOT NULL, outcome STRING NOT NULL,
      message STRING, metadata STRING, event_at TIMESTAMP""",
        "Each retrieval attempt: fetched, unchanged, blocked, licence_rejected or failed, with HTTP/API metadata.",
    ),
    _t(
        "ops",
        "asset_manifest",
        """
      source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT NOT NULL, release_label STRING,
      retrieval_mode STRING NOT NULL, landing_page STRING, asset_url STRING, final_url STRING,
      api_item_id STRING, api_service_url STRING, api_layer_id INT, api_record_count BIGINT,
      api_item_modified TIMESTAMP, api_data_last_edit TIMESTAMP, http_metadata STRING,
      retrieved_at TIMESTAMP, published STRING, file_name STRING, byte_size BIGINT, sha256 STRING NOT NULL,
      raw_path STRING, archive_path STRING NOT NULL, licence_id STRING, licence_evidence STRING,
      licence_verified BOOLEAN, attribution STRING, status STRING NOT NULL, status_reason STRING,
      first_seen_run_id STRING NOT NULL, last_checked_run_id STRING, superseded_by_version INT,
      created_at TIMESTAMP""",
        "Immutable record of every source asset version: URLs, checksums or API identifiers, licence and status.",
    ),
    _t(
        "ops",
        "asset_profile",
        """
      source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT NOT NULL, part STRING NOT NULL,
      max_row INT, max_col INT, sample STRING, profiled_at TIMESTAMP""",
        "Shape of each sheet or table in an asset version, used as contract evidence.",
    ),
    _t(
        "ops",
        "parse_log",
        """
      run_id STRING NOT NULL, source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT NOT NULL,
      contract STRING, parser_version STRING, status STRING NOT NULL, rows_out BIGINT, rows_rejected BIGINT,
      message STRING, parsed_at TIMESTAMP""",
        "Parser outcome per asset version. Status parsed, quarantined or failed.",
    ),
    _t(
        "ops",
        "reject",
        """
      run_id STRING NOT NULL, source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT,
      record_locator STRING, reason_code STRING NOT NULL, reason STRING, original_value STRING,
      evidence STRING, rejected_at TIMESTAMP""",
        "Records and assets that failed a contract rule, with the locator and evidence.",
    ),
    _t(
        "ops",
        "quality_result",
        """
      run_id STRING NOT NULL, release_id STRING NOT NULL, check_id STRING NOT NULL, severity STRING NOT NULL,
      status STRING NOT NULL, scope STRING, observed STRING, expected STRING, message STRING,
      checked_at TIMESTAMP""",
        "Quality gate results for each candidate release. A failed blocking check stops publication.",
    ),
    _t(
        "ops",
        "reconciliation",
        """
      run_id STRING NOT NULL, release_id STRING NOT NULL, metric_id STRING NOT NULL, company_code STRING,
      report_year INT, preferred_source_id STRING, preferred_value DOUBLE, other_source_id STRING,
      other_value DOUBLE, tolerance DOUBLE, status STRING NOT NULL""",
        "Values reported by more than one source for the same metric, company and year.",
    ),
    _t(
        "ops",
        "release",
        """
      release_id STRING NOT NULL, run_id STRING NOT NULL, status STRING NOT NULL, created_at TIMESTAMP,
      published_at TIMESTAMP, summary STRING""",
        "Every Gold release: candidate, published, rejected, superseded or rolled_back.",
    ),
    _t(
        "ops",
        "published_release",
        """
      release_id STRING NOT NULL, published_at TIMESTAMP, published_by STRING, previous_release_id STRING""",
        "Exactly one row: the Gold release that every Gold view reads. Switching it is one atomic commit.",
    ),
    _t(
        "ops",
        "test_result",
        """
      run_id STRING NOT NULL, suite STRING NOT NULL, test_id STRING NOT NULL, outcome STRING NOT NULL,
      duration_seconds DOUBLE, message STRING, recorded_at TIMESTAMP""",
        "Validation job test outcomes.",
    ),
)

SILVER_TABLES = (
    _t(
        "silver",
        "stream_apr_record",
        PROVENANCE_COLS
        + """,
      item_id STRING NOT NULL, object_id BIGINT NOT NULL, company_code_raw STRING, company_code STRING,
      reporting_area STRING, financial_year_raw STRING, report_year INT, item_reference STRING,
      line_description STRING, units STRING, decimal_places INT, rag_reference STRING, attribute_raw STRING,
      attribute STRING, value DOUBLE, value_raw STRING, rag_version STRING""",
        "Stream APR API records, one row per record and source version.",
    ),
    _t(
        "silver",
        "ofwat_wcpr_record",
        PROVENANCE_COLS
        + """,
      company_name_raw STRING, company_code_raw STRING, company_code STRING, item_reference STRING,
      short_description STRING, rag_reference STRING, financial_year_raw STRING, report_year INT,
      value DOUBLE, value_raw STRING, units STRING""",
        "Ofwat Water Company Performance Report normalised open data, one row per published value and release.",
    ),
    _t(
        "silver",
        "rag_guideline",
        PROVENANCE_COLS
        + """,
      rag_version STRING, published STRING, document_path STRING, page_count INT, licence_statement STRING""",
        "Versions of the RAG 4 APR table-definition guideline (reference metadata, not observations).",
    ),
    _t(
        "silver",
        "licence_evidence",
        """
      source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT NOT NULL, sha256 STRING NOT NULL,
      licence_id STRING, statement STRING, locator STRING, verified BOOLEAN NOT NULL, run_id STRING NOT NULL""",
        "Licence statements found inside conditional assets.",
    ),
)

OUTCOME_SILVER_TABLES = (
    _t(
        "silver",
        "ofwat_pr19_pc",
        PROVENANCE_COLS
        + """,
      company_code STRING NOT NULL, pc_id STRING NOT NULL, pc_name STRING, pc_short_description STRING,
      apr_table STRING, pc_type STRING, outcome STRING, odi_type STRING, odi_form STRING, odi_timing STRING,
      unit_raw STRING, unit_description STRING, direction STRING, primary_category STRING, comparable_pc STRING,
      measure STRING NOT NULL, report_year INT NOT NULL, value DOUBLE, value_raw STRING, unit STRING,
      price_base STRING, pcl_met STRING, sharing_basis STRING""",
        "PR19 (2020-25) performance commitments: level, actual, PCL met and ODI payment by company and year.",
    ),
    _t(
        "silver",
        "ofwat_pr19_price_control_odi",
        PROVENANCE_COLS
        + """,
      company_code STRING NOT NULL, item_reference STRING NOT NULL, description STRING, report_year INT NOT NULL,
      value DOUBLE, value_raw STRING, unit STRING, price_base STRING, sharing_basis STRING""",
        "PR19 net ODI payments by price control and year, after the aggregate sharing mechanism.",
    ),
    _t(
        "silver",
        "ofwat_pr24_pc",
        PROVENANCE_COLS
        + """,
      company_code STRING NOT NULL, pc_name STRING NOT NULL, setting_level STRING, unit_raw STRING,
      measure STRING NOT NULL, report_year INT NOT NULL, value DOUBLE, value_raw STRING, unit STRING""",
        "PR24 (2025-30) performance commitment levels, deadbands and enhanced thresholds by company and year.",
    ),
)


def _release_stores() -> tuple[str, ...]:
    from . import gold

    return tuple(gold.store_ddl(name) for name in gold.RELEASE_STORES)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "ops and source-specific Silver tables", OPS_TABLES + SILVER_TABLES),
    Migration(2, "PR19 and PR24 outcome Silver tables", OUTCOME_SILVER_TABLES),
    Migration(
        3,
        "Gold release stores; reconciliation moves into the release",
        _release_stores() + ("DROP TABLE IF EXISTS {ops}.reconciliation",),
    ),
    Migration(
        4,
        "Regional reporting areas in WCPR; the financial year named inside Stream attributes",
        (
            AddColumn(
                "silver",
                "ofwat_wcpr_record",
                "reporting_area",
                "STRING",
                "whole_company, or the region a regional WCPR row reports (reporting_areas.csv)",
            ),
            AddColumn(
                "silver",
                "stream_apr_record",
                "attribute_financial_year",
                "INT",
                "Financial-year end named inside the attribute, e.g. actual (2023-24) -> 2024",
            ),
            "UPDATE {silver}.ofwat_wcpr_record SET reporting_area = 'whole_company' WHERE reporting_area IS NULL",
        ),
    ),
)


def render(statement: str, target) -> str:
    return statement.format(ops=target.schema("ops"), silver=target.schema("silver"), gold=target.schema("gold"))


def pending(applied: set[int]) -> list[Migration]:
    versions = [m.version for m in MIGRATIONS]
    if versions != sorted(versions) or len(set(versions)) != len(versions):
        raise RuntimeError("migration versions must be unique and ascending")
    return [m for m in MIGRATIONS if m.version not in applied]


def apply_migrations(spark, target, run_id: str, upto: int | None = None) -> list[int]:
    """Apply every pending migration (or those up to `upto`). Returns the versions applied."""
    ops = target.schema("ops")
    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {ops}.schema_migration (version INT NOT NULL, description STRING, "
        "applied_at TIMESTAMP, run_id STRING) COMMENT 'Applied schema migrations.'"
    )
    applied = {r["version"] for r in spark.sql(f"SELECT version FROM {ops}.schema_migration").collect()}
    done = []
    for m in pending(applied):
        if upto is not None and m.version > upto:
            break
        LOG.info("applying migration %d: %s", m.version, m.description)
        for stmt in m.statements:
            if isinstance(stmt, AddColumn):
                fqn = target.table(stmt.layer, stmt.table)
                if stmt.column not in spark.table(fqn).columns:
                    spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({stmt.column} {stmt.type} COMMENT '{stmt.comment}')")
                continue
            spark.sql(render(stmt, target))
        spark.sql(
            f"INSERT INTO {ops}.schema_migration VALUES ({m.version}, '{m.description}', "
            f"current_timestamp(), '{run_id}')"
        )
        done.append(m.version)
    return done
