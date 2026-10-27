"""Spark table helpers. Rows are coerced to the target table's own schema."""

from __future__ import annotations

import datetime as dt
import json
import logging

from .runtime import utcnow

LOG = logging.getLogger("water_performance.store")


def _coerce(value, dtype: str):
    if value is None:
        return None
    if dtype == "timestamp" and isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dtype == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if dtype in ("int", "bigint") and not isinstance(value, bool):
        return int(value)
    if dtype == "double":
        return float(value)
    if dtype == "string" and not isinstance(value, str):
        return json.dumps(value, default=str, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    return value


def append(spark, fqn: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    schema = spark.table(fqn).schema
    types = [(f.name, f.dataType.simpleString()) for f in schema]
    data = [tuple(_coerce(r.get(name), t) for name, t in types) for r in rows]
    spark.createDataFrame(data, schema).write.mode("append").saveAsTable(fqn)
    return len(rows)


def append_pandas(spark, fqn: str, pdf) -> int:
    """Append a pandas DataFrame, aligning columns to the table and filling missing ones with nulls."""
    if pdf is None or len(pdf) == 0:
        return 0
    schema = spark.table(fqn).schema
    for f in schema:
        if f.name not in pdf.columns:
            pdf[f.name] = None
    pdf = pdf[[f.name for f in schema]]
    pdf = pdf.astype(object).where(pdf.notna(), None)
    spark.createDataFrame(pdf, schema).write.mode("append").saveAsTable(fqn)
    return len(pdf)


def rows(spark, query: str) -> list[dict]:
    return [r.asDict(recursive=True) for r in spark.sql(query).collect()]


def record_stage(
    spark,
    target,
    run_id: str,
    stage: str,
    status: str,
    started_at,
    source_id: str | None = None,
    message: str = "",
    metrics: dict | None = None,
) -> None:
    append(
        spark,
        target.table("ops", "pipeline_run"),
        [
            {
                "run_id": run_id,
                "stage": stage,
                "source_id": source_id,
                "status": status,
                "message": (message or "")[:8000],
                "metrics": metrics or {},
                "started_at": started_at,
                "finished_at": utcnow(),
            }
        ],
    )
    LOG.info("stage=%s source=%s status=%s %s", stage, source_id, status, (message or "")[:500])
