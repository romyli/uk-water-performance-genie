"""Build a Gold candidate release from Silver and the governed configuration, and publish it.

Gold objects are views. Each reads a release store in the Silver schema (`release_<name>`) filtered
to the single release named in `ops.published_release`. Building a candidate writes rows under a
new `release_id` that no view can see yet; publishing replaces the one pointer row, so every Gold
view switches to the new release in one commit. Rollback points the row at an earlier release.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import periods, registry
from .registry import CONFIG_DIR
from .runtime import sql_literal

SOURCE_PRECEDENCE = {"OFWAT_WCPR": 1, "STREAM_APR": 2, "COMPANY_APR": 3}

RELEASE_STORES = {
    "dim_company": """company_code STRING NOT NULL, company_name STRING NOT NULL, service_type STRING NOT NULL,
        jurisdiction STRING NOT NULL, aliases STRING, notes STRING""",
    "dim_period": """report_year INT NOT NULL, financial_year STRING NOT NULL, fy_start_date DATE, fy_end_date DATE,
        amp STRING, price_review STRING, period_basis STRING""",
    "dim_source": """source_id STRING NOT NULL, title STRING, priority STRING, status STRING, publisher STRING,
        landing_page STRING, jurisdiction STRING, licence_id STRING, licence_name STRING, licence_url STRING,
        attribution STRING, third_party_rights STRING, reporting_basis STRING, retrieval_mode STRING,
        hold_reason STRING, precedence INT""",
    "dim_metric": """metric_id STRING NOT NULL, metric_name STRING NOT NULL, theme STRING, description STRING,
        unit STRING, price_base STRING, direction STRING, aggregation STRING, service_scope STRING,
        regulatory_period STRING, comparability_status STRING NOT NULL, comparability_reason STRING,
        definition_source STRING, synonyms STRING""",
    "metric_source_map": """metric_id STRING NOT NULL, source_id STRING NOT NULL, item_reference STRING,
        rag_reference STRING, attribute STRING, line_description STRING, source_unit STRING,
        conversion_factor DOUBLE""",
    "deployment_context": """company_code STRING NOT NULL, company_name STRING NOT NULL, service_type STRING NOT NULL,
        jurisdiction STRING NOT NULL, interpretation STRING NOT NULL, configured_by STRING""",
    "fact_reported_performance": """observation_id STRING NOT NULL, source_id STRING NOT NULL,
        company_code STRING NOT NULL, reporting_area STRING NOT NULL, report_year INT NOT NULL,
        financial_year STRING NOT NULL, metric_id STRING, source_item_reference STRING, rag_reference STRING,
        original_label STRING, attribute STRING, value DOUBLE NOT NULL, unit STRING, source_unit STRING,
        price_base STRING, is_comparable BOOLEAN NOT NULL, comparability_reason STRING,
        is_preferred_source BOOLEAN NOT NULL, previous_release_value DOUBLE, restated BOOLEAN,
        asset_key STRING NOT NULL, asset_version INT NOT NULL, sha256 STRING NOT NULL, release_label STRING,
        record_locator STRING NOT NULL, parser_version STRING, rag_version STRING,
        attribute_financial_year INT""",
    "fact_performance_commitment": """commitment_id STRING NOT NULL, source_id STRING NOT NULL,
        price_review STRING NOT NULL, amp STRING NOT NULL, company_code STRING NOT NULL, report_year INT NOT NULL,
        financial_year STRING NOT NULL, pc_id STRING, pc_name STRING NOT NULL, pc_type STRING,
        primary_category STRING, comparable_pc STRING, unit STRING, unit_description STRING, direction STRING,
        commitment_level DOUBLE, actual_performance DOUBLE, pcl_met STRING, baseline_level DOUBLE,
        enhanced_threshold DOUBLE, standard_collar DOUBLE, standard_cap DOUBLE,
        underperformance_deadband DOUBLE, outperformance_deadband DOUBLE, setting_level STRING,
        asset_key STRING NOT NULL, asset_version INT NOT NULL, sha256 STRING NOT NULL, release_label STRING,
        record_locator STRING NOT NULL""",
    "fact_odi": """odi_id STRING NOT NULL, source_id STRING NOT NULL, price_review STRING NOT NULL,
        company_code STRING NOT NULL, report_year INT NOT NULL, financial_year STRING NOT NULL, level STRING NOT NULL,
        pc_id STRING, pc_name STRING, price_control STRING, item_reference STRING, payment_gbp_m DOUBLE NOT NULL,
        price_base STRING NOT NULL, sharing_basis STRING, asset_key STRING NOT NULL, asset_version INT NOT NULL,
        sha256 STRING NOT NULL, release_label STRING, record_locator STRING NOT NULL""",
    "reconciliation": """metric_id STRING NOT NULL, company_code STRING NOT NULL, report_year INT NOT NULL,
        preferred_source_id STRING, preferred_value DOUBLE, other_source_id STRING, other_value DOUBLE,
        tolerance DOUBLE, status STRING NOT NULL""",
    "source_attribution": """source_id STRING NOT NULL, asset_key STRING NOT NULL, asset_version INT NOT NULL,
        release_label STRING, attribution_text STRING NOT NULL, licence_id STRING NOT NULL, licence_name STRING,
        licence_url STRING, landing_page STRING, asset_url STRING, third_party_rights STRING, retrieved_at TIMESTAMP,
        sha256 STRING""",
}

# Gold objects that are plain projections of a release store.
PROJECTED = tuple(RELEASE_STORES)


def store_ddl(name: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {{silver}}.release_{name} (release_id STRING NOT NULL, "
        f"{RELEASE_STORES[name]}) COMMENT 'Release store for gold.{name}; read only through the Gold view.'"
    )


def projection_view(name: str, comment: str) -> str:
    cols = ", ".join(c.strip().split()[0] for c in _split_cols(RELEASE_STORES[name]))
    return (
        f"CREATE OR REPLACE VIEW {{gold}}.{name} COMMENT {sql_literal(comment)} AS "
        f"SELECT {cols} FROM {{silver}}.release_{name} "
        f"WHERE release_id = (SELECT release_id FROM {{ops}}.published_release)"
    )


def _split_cols(ddl: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in ddl:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [c.strip() for c in out if c.strip()]


# --- Configuration --------------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricConfig:
    metrics: list[dict]
    rules: list[dict]


def load_metrics(config_dir: Path | str | None = None) -> MetricConfig:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    doc = yaml.safe_load((base / "metrics.yml").read_text(encoding="utf-8")) or {}
    metrics, rules, seen = [], [], set()
    for m in doc.get("metrics") or []:
        mid = m["metric_id"]
        if mid in seen:
            raise ValueError(f"metric {mid} is defined twice")
        seen.add(mid)
        status = m.get("comparability", "not_comparable")
        if status not in ("comparable", "not_comparable"):
            raise ValueError(f"metric {mid}: comparability must be comparable or not_comparable")
        if status == "comparable":
            missing = [
                k for k in ("unit", "price_base", "direction", "service_scope", "definition_source") if not m.get(k)
            ]
            if missing:
                raise ValueError(f"metric {mid}: comparable metrics must record {missing}")
        if m.get("service_scope") not in (None, "all", "water", "wastewater"):
            raise ValueError(f"metric {mid}: service_scope must be all, water or wastewater")
        if status == "not_comparable" and not m.get("comparability_reason"):
            raise ValueError(f"metric {mid}: say why it is not comparable")
        metrics.append(
            {
                "metric_id": mid,
                "metric_name": m["name"],
                "theme": m.get("theme"),
                "description": m.get("description"),
                "unit": m.get("unit"),
                "price_base": m.get("price_base"),
                "direction": m.get("direction"),
                "aggregation": m.get("aggregation", "none"),
                "service_scope": m.get("service_scope", "all"),
                "regulatory_period": m.get("regulatory_period", "all"),
                "comparability_status": status,
                "comparability_reason": m.get("comparability_reason"),
                "definition_source": m.get("definition_source"),
                "synonyms": ", ".join(m.get("synonyms") or []),
            }
        )
        for s in m.get("sources") or []:
            if s["source_id"] not in SOURCE_PRECEDENCE:
                raise ValueError(f"metric {mid}: {s['source_id']} is not an observation source")
            if not any(s.get(k) for k in ("item_reference", "rag_reference", "line_description")):
                raise ValueError(
                    f"metric {mid}: each source rule needs item_reference, rag_reference or line_description"
                )
            rules.append(
                {
                    "metric_id": mid,
                    "source_id": s["source_id"],
                    "item_reference": s.get("item_reference"),
                    "rag_reference": s.get("rag_reference"),
                    "attribute": s.get("attribute"),
                    "line_description": s.get("line_description"),
                    "source_unit": s.get("unit"),
                    "conversion_factor": float(s.get("conversion_factor", 1.0)),
                }
            )
    return MetricConfig(metrics, rules)


def config_rows(company_code: str) -> dict[str, list[dict]]:
    reg = registry.load_registry()
    companies = registry.load_companies()
    company = registry.resolve_company(company_code, companies)
    mc = load_metrics()
    sources = []
    for s in reg.sources.values():
        lic = s.get("licence") or {}
        known = reg.licences.get(lic.get("id")) or {}
        sources.append(
            {
                "source_id": s.source_id,
                "title": s.get("title"),
                "priority": s.get("priority"),
                "status": s.status,
                "publisher": s.get("publisher"),
                "landing_page": s.get("landing_page"),
                "jurisdiction": s.get("jurisdiction"),
                "licence_id": lic.get("id"),
                "licence_name": known.get("name"),
                "licence_url": known.get("url"),
                "attribution": s.get("attribution"),
                "third_party_rights": s.get("third_party_rights"),
                "reporting_basis": s.get("reporting_basis"),
                "retrieval_mode": s.get("retrieval_mode"),
                "hold_reason": s.get("hold_reason"),
                "precedence": SOURCE_PRECEDENCE.get(s.source_id),
            }
        )
    places = {"england_wales": "England and Wales", "england": "England", "wales": "Wales"}
    place = places.get(company.jurisdiction, company.jurisdiction)
    interpretation = (
        f"'We', 'us', 'our' and 'my company' mean {company.company_name} ({company.company_code}), "
        f"a {company.service_type.replace('_', ' ')} company in {place}."
    )
    return {
        "dim_company": [
            {
                "company_code": c.company_code,
                "company_name": c.company_name,
                "service_type": c.service_type,
                "jurisdiction": c.jurisdiction,
                "aliases": ", ".join(c.aliases),
                "notes": c.notes or None,
            }
            for c in companies.values()
        ],
        "dim_period": periods.period_rows(2011, 2030),
        "dim_source": sources,
        "dim_metric": mc.metrics,
        "metric_source_map": mc.rules,
        "deployment_context": [
            {
                "company_code": company.company_code,
                "company_name": company.company_name,
                "service_type": company.service_type,
                "jurisdiction": company.jurisdiction,
                "interpretation": interpretation,
                "configured_by": "bundle variable company_code",
            }
        ],
    }
