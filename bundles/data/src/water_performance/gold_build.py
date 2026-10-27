"""SQL that turns Silver into a Gold candidate release, plus the curated Gold views.

Statements use `{ops}`, `{silver}` and `{gold}` for fully qualified schemas and `{release}` for the
candidate release ID. Configuration tables (`cfg_*`) are temporary views created from the
governed YAML/CSV files at publish time.
"""

from __future__ import annotations

NORM_UNIT = "lower(regexp_replace(coalesce({x}, ''), '\\\\s+', ''))"


def norm_unit(x: str) -> str:
    return NORM_UNIT.format(x=x)


# The version of each asset that the release uses: the newest licence-verified version that parsed
# successfully. A newer version that failed its contract therefore never removes the last valid one.
CURRENT_ASSETS = """
  SELECT m.source_id, m.asset_key, m.asset_version, m.retrieved_at, coalesce(o.release_rank, 0) AS release_rank
  FROM {ops}.asset_manifest m
  JOIN (SELECT source_id, asset_key, max(asset_version) AS asset_version
        FROM {ops}.parse_log WHERE status = 'parsed' GROUP BY source_id, asset_key) p
    ON p.source_id = m.source_id AND p.asset_key = m.asset_key AND p.asset_version = m.asset_version
  LEFT JOIN cfg_asset_order o ON o.source_id = m.source_id AND o.asset_key = m.asset_key
  WHERE m.licence_verified AND m.status <> 'withdrawn'
"""

CONFORM_SQL = """
CREATE OR REPLACE TABLE {silver}.conformed_observation
COMMENT 'Current, licence-verified observations from every reported-performance source, conformed to canonical company, reporting area and financial year. Rebuilt by the conform task.'
AS
WITH cur AS ({current}),
wcpr AS (
  SELECT w.*, cur.release_rank,
         row_number() OVER (PARTITION BY w.company_code, w.reporting_area, w.item_reference, w.report_year
                            ORDER BY cur.release_rank DESC, cur.retrieved_at DESC) AS rn,
         lead(w.value) OVER (PARTITION BY w.company_code, w.reporting_area, w.item_reference, w.report_year
                             ORDER BY cur.release_rank DESC, cur.retrieved_at DESC) AS previous_value
  FROM {silver}.ofwat_wcpr_record w
  JOIN cur ON cur.source_id = w.source_id AND cur.asset_key = w.asset_key AND cur.asset_version = w.asset_version
)
SELECT 'OFWAT_WCPR' AS source_id, w.company_code, coalesce(w.reporting_area, 'whole_company') AS reporting_area,
       w.report_year, p.financial_year, w.item_reference AS source_item_reference, w.rag_reference,
       w.short_description AS original_label, CAST(NULL AS STRING) AS attribute,
       CAST(NULL AS INT) AS attribute_financial_year, w.value, w.units AS source_unit,
       CAST(NULL AS INT) AS decimal_places, w.previous_value, w.asset_key, w.asset_version, w.sha256,
       w.release_label, w.record_locator, w.parser_version, CAST(NULL AS STRING) AS rag_version
FROM wcpr w JOIN cfg_dim_period p ON p.report_year = w.report_year
WHERE w.rn = 1
UNION ALL
SELECT s.source_id, s.company_code, s.reporting_area, s.report_year, p.financial_year, s.item_reference,
       s.rag_reference, s.line_description, s.attribute, s.attribute_financial_year, s.value, s.units,
       s.decimal_places, CAST(NULL AS DOUBLE), s.asset_key, s.asset_version, s.sha256, s.release_label,
       s.record_locator, s.parser_version, s.rag_version
FROM {silver}.stream_apr_record s
JOIN cur ON cur.source_id = s.source_id AND cur.asset_key = s.asset_key AND cur.asset_version = s.asset_version
JOIN cfg_dim_period p ON p.report_year = s.report_year
UNION ALL
SELECT c.source_id, c.company_code, c.reporting_area, c.report_year, p.financial_year, c.item_reference,
       c.rag_reference, c.line_description, c.attribute, c.attribute_financial_year, c.value, c.units,
       c.decimal_places, CAST(NULL AS DOUBLE), c.asset_key, c.asset_version, c.sha256, c.release_label,
       c.record_locator, c.parser_version, c.rag_version
FROM {silver}.company_apr_record c
JOIN cur ON cur.source_id = c.source_id AND cur.asset_key = c.asset_key AND cur.asset_version = c.asset_version
JOIN cfg_dim_period p ON p.report_year = c.report_year
"""

# One row per published value. WCPR releases overlap (each repeats earlier years), so conform keeps the
# latest release's value and the previous release's value to show restatements.
REPORTED_BASE = """
WITH obs AS (SELECT * FROM {silver}.conformed_observation),
mapped AS (
  SELECT o.*, r.metric_id, r.source_unit AS rule_unit, r.conversion_factor,
         row_number() OVER (PARTITION BY o.source_id, o.company_code, o.reporting_area, o.report_year,
                            o.source_item_reference, o.rag_reference, o.original_label, o.attribute
                            ORDER BY r.metric_id) AS rule_rank
  FROM obs o
  LEFT JOIN cfg_metric_source_map r
    ON r.source_id = o.source_id
   AND (r.item_reference IS NULL OR r.item_reference = o.source_item_reference)
   AND (r.rag_reference IS NULL OR r.rag_reference = o.rag_reference)
   AND (r.attribute IS NULL OR lower(replace(r.attribute, '{{financial_year}}', o.financial_year)) = lower(o.attribute))
   AND (r.line_description IS NULL OR lower(r.line_description) = lower(o.original_label))
)
SELECT m.*, dm.comparability_status, dm.comparability_reason AS metric_reason, dm.service_scope,
       dm.regulatory_period, dm.unit AS metric_unit, dm.price_base AS metric_price_base, c.service_type, p.amp
FROM mapped m
JOIN cfg_dim_company c ON c.company_code = m.company_code
JOIN cfg_dim_period p ON p.report_year = m.report_year
LEFT JOIN cfg_dim_metric dm ON dm.metric_id = m.metric_id
WHERE m.rule_rank = 1
"""

REPORTED_INSERT = """
INSERT INTO {silver}.release_fact_reported_performance
WITH base AS ({base}),
judged AS (
  SELECT *,
    CASE
      WHEN metric_id IS NULL THEN 'No approved metric mapping: shown as published, not ranked.'
      WHEN comparability_status <> 'comparable' THEN metric_reason
      WHEN reporting_area <> 'whole_company' THEN 'Regional figure; only whole-company values are compared.'
      WHEN rule_unit IS NOT NULL AND {unit_check} THEN concat('Published unit "', coalesce(source_unit, ''),
           '" differs from the unit approved for this metric ("', rule_unit, '").')
      WHEN service_scope = 'wastewater' AND service_type = 'water_only' THEN 'Wastewater measure; does not apply to a water-only company.'
      WHEN regulatory_period <> 'all' AND coalesce(amp, '') <> regulatory_period THEN concat('Definition applies to ', regulatory_period, ' only.')
      ELSE NULL
    END AS reason
  FROM base
),
ranked AS (
  SELECT *, row_number() OVER (PARTITION BY coalesce(metric_id, sha2(concat_ws('|', source_id, record_locator), 256)),
                               company_code, reporting_area, report_year
                               ORDER BY CASE source_id WHEN 'OFWAT_WCPR' THEN 1 WHEN 'STREAM_APR' THEN 2 ELSE 3 END) AS src_rank
  FROM judged
)
SELECT '{release}',
       sha2(concat_ws('|', source_id, company_code, reporting_area, CAST(report_year AS STRING),
            coalesce(source_item_reference, ''), coalesce(rag_reference, ''), coalesce(original_label, ''),
            coalesce(attribute, '')), 256),
       source_id, company_code, reporting_area, report_year, financial_year, metric_id, source_item_reference,
       rag_reference, original_label, attribute,
       value * coalesce(conversion_factor, 1.0),
       CASE WHEN metric_id IS NOT NULL THEN metric_unit ELSE source_unit END, source_unit,
       CASE WHEN metric_id IS NOT NULL THEN metric_price_base END,
       reason IS NULL, reason,
       metric_id IS NULL OR src_rank = 1,
       previous_value, previous_value IS NOT NULL AND abs(previous_value - value) > 1e-9,
       asset_key, asset_version, sha256, release_label, record_locator, parser_version, rag_version,
       attribute_financial_year
FROM ranked
"""

COMMITMENT_INSERT = """
INSERT INTO {silver}.release_fact_performance_commitment
WITH cur AS ({current}),
pr19 AS (
  SELECT s.company_code, s.pc_id, s.report_year, first(s.pc_name) pc_name, first(s.pc_type) pc_type,
         first(s.primary_category) primary_category, first(s.comparable_pc) comparable_pc, first(s.unit_raw) unit_raw,
         first(s.unit_description) unit_description, first(s.direction) direction,
         max(CASE WHEN s.measure = 'pcl' THEN s.value END) commitment_level,
         max(CASE WHEN s.measure = 'actual' THEN s.value END) actual_performance,
         max(CASE WHEN s.measure IN ('pcl', 'actual') THEN s.pcl_met END) pcl_met,
         max(CASE WHEN s.measure = 'standard_collar' THEN s.value END) standard_collar,
         max(CASE WHEN s.measure = 'standard_cap' THEN s.value END) standard_cap,
         max(CASE WHEN s.measure = 'underperformance_deadband' THEN s.value END) underperformance_deadband,
         max(CASE WHEN s.measure = 'outperformance_deadband' THEN s.value END) outperformance_deadband,
         max(CASE WHEN s.measure IN ('pcl', 'actual') THEN s.unit END) unit_loaded,
         first(s.asset_key) asset_key, first(s.asset_version) asset_version, first(s.sha256) sha256,
         first(s.release_label) release_label,
         concat_ws(' | ', collect_set(regexp_extract(s.record_locator, '^(.*;row=[0-9]+)', 1))) record_locator
  FROM {silver}.ofwat_pr19_pc s
  JOIN cur ON cur.source_id = s.source_id AND cur.asset_key = s.asset_key AND cur.asset_version = s.asset_version
  WHERE s.measure <> 'odi_payment'
  GROUP BY s.company_code, s.pc_id, s.report_year
),
pr24 AS (
  SELECT s.company_code, s.pc_name, s.report_year, first(s.setting_level) setting_level, first(s.unit_raw) unit_raw,
         max(CASE WHEN s.measure = 'pcl' THEN s.value END) commitment_level,
         max(CASE WHEN s.measure = 'deadband' THEN s.value END) deadband,
         max(CASE WHEN s.measure = 'enhanced_threshold' THEN s.value END) enhanced_threshold,
         max(CASE WHEN s.measure = 'pcl' THEN s.unit END) unit_loaded,
         first(s.asset_key) asset_key, first(s.asset_version) asset_version, first(s.sha256) sha256,
         first(s.release_label) release_label,
         concat_ws(' | ', collect_set(regexp_extract(s.record_locator, '^(.*;row=[0-9]+)', 1))) record_locator
  FROM {silver}.ofwat_pr24_pc s
  JOIN cur ON cur.source_id = s.source_id AND cur.asset_key = s.asset_key AND cur.asset_version = s.asset_version
  WHERE s.measure IN ('pcl', 'deadband', 'enhanced_threshold')
  GROUP BY s.company_code, s.pc_name, s.report_year
),
pr24_baseline AS (
  SELECT s.company_code, s.pc_name, max(s.value) baseline_level
  FROM {silver}.ofwat_pr24_pc s
  JOIN cur ON cur.source_id = s.source_id AND cur.asset_key = s.asset_key AND cur.asset_version = s.asset_version
  WHERE s.measure = 'pcl_baseline' GROUP BY s.company_code, s.pc_name
)
SELECT '{release}', sha2(concat_ws('|', 'PR19', company_code, pc_id, CAST(report_year AS STRING)), 256),
       'OFWAT_PR19_ODI', 'PR19', 'AMP7', company_code, report_year, p.financial_year, pc_id, pc_name, pc_type,
       primary_category, comparable_pc, coalesce(unit_loaded, unit_raw), unit_description, direction,
       commitment_level, actual_performance, pcl_met, CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), standard_collar,
       standard_cap, underperformance_deadband, outperformance_deadband, CAST(NULL AS STRING), asset_key,
       asset_version, sha256, release_label, record_locator
FROM pr19 JOIN cfg_dim_period p USING (report_year)
UNION ALL
SELECT '{release}', sha2(concat_ws('|', 'PR24', pr24.company_code, pr24.pc_name, CAST(report_year AS STRING)), 256),
       'OFWAT_PR24_OUTCOMES', 'PR24', 'AMP8', pr24.company_code, report_year, p.financial_year, CAST(NULL AS STRING),
       pr24.pc_name, CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), coalesce(unit_loaded, unit_raw),
       unit_raw, CAST(NULL AS STRING), commitment_level, CAST(NULL AS DOUBLE), CAST(NULL AS STRING), b.baseline_level,
       enhanced_threshold, CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), deadband, CAST(NULL AS DOUBLE), setting_level,
       asset_key, asset_version, sha256, release_label, record_locator
FROM pr24 JOIN cfg_dim_period p USING (report_year)
LEFT JOIN pr24_baseline b ON b.company_code = pr24.company_code AND b.pc_name = pr24.pc_name
WHERE report_year >= 2026
"""

ODI_INSERT = """
INSERT INTO {silver}.release_fact_odi
WITH cur AS ({current})
SELECT '{release}', sha2(concat_ws('|', 'pc', s.company_code, s.pc_id, CAST(s.report_year AS STRING)), 256),
       s.source_id, 'PR19', s.company_code, s.report_year, p.financial_year, 'performance_commitment', s.pc_id,
       s.pc_name, CAST(NULL AS STRING), CAST(NULL AS STRING), s.value, s.price_base, s.sharing_basis, s.asset_key,
       s.asset_version, s.sha256, s.release_label, s.record_locator
FROM {silver}.ofwat_pr19_pc s
JOIN cur ON cur.source_id = s.source_id AND cur.asset_key = s.asset_key AND cur.asset_version = s.asset_version
JOIN cfg_dim_period p ON p.report_year = s.report_year
WHERE s.measure = 'odi_payment' AND s.price_base IS NOT NULL
UNION ALL
SELECT '{release}', sha2(concat_ws('|', 'control', s.company_code, s.item_reference, CAST(s.report_year AS STRING)), 256),
       s.source_id, 'PR19', s.company_code, s.report_year, p.financial_year, 'price_control', CAST(NULL AS STRING),
       CAST(NULL AS STRING), trim(regexp_extract(s.description, '-\\\\s*([^-]+)$', 1)), s.item_reference, s.value,
       s.price_base, s.sharing_basis, s.asset_key, s.asset_version, s.sha256, s.release_label, s.record_locator
FROM {silver}.ofwat_pr19_price_control_odi s
JOIN cur ON cur.source_id = s.source_id AND cur.asset_key = s.asset_key AND cur.asset_version = s.asset_version
JOIN cfg_dim_period p ON p.report_year = s.report_year
WHERE s.price_base IS NOT NULL
"""

# WCPR is preferred; Stream values for the same metric, company and year are compared with it.
RECONCILIATION_INSERT = """
INSERT INTO {silver}.release_reconciliation
WITH f AS (
  SELECT f.*, s.decimal_places FROM {silver}.release_fact_reported_performance f
  LEFT JOIN {silver}.stream_apr_record s ON f.source_id = 'STREAM_APR' AND s.asset_key = f.asset_key
       AND s.asset_version = f.asset_version AND s.record_locator = f.record_locator
  WHERE f.release_id = '{release}' AND f.metric_id IS NOT NULL AND f.reporting_area = 'whole_company'
)
SELECT '{release}', a.metric_id, a.company_code, a.report_year, a.source_id, a.value, b.source_id, b.value,
       greatest(0.5 * power(10, -coalesce(b.decimal_places, 3)), 0.005 * abs(a.value)) AS tolerance,
       CASE WHEN abs(a.value - b.value) <= greatest(0.5 * power(10, -coalesce(b.decimal_places, 3)), 0.005 * abs(a.value))
            THEN 'agree' ELSE 'disagree' END
FROM f a JOIN f b ON a.metric_id = b.metric_id AND a.company_code = b.company_code AND a.report_year = b.report_year
 AND a.is_preferred_source AND NOT b.is_preferred_source
"""

ATTRIBUTION_INSERT = """
INSERT INTO {silver}.release_source_attribution
WITH used AS (
  SELECT DISTINCT source_id, asset_key, asset_version FROM {silver}.release_fact_reported_performance WHERE release_id = '{release}'
  UNION SELECT DISTINCT source_id, asset_key, asset_version FROM {silver}.release_fact_performance_commitment WHERE release_id = '{release}'
  UNION SELECT DISTINCT source_id, asset_key, asset_version FROM {silver}.release_fact_odi WHERE release_id = '{release}'
)
SELECT '{release}', m.source_id, m.asset_key, m.asset_version, m.release_label,
       replace(replace(replace(coalesce(s.attribution, ''), '{{company_name}}', coalesce(i.company_name, '')),
               '{{financial_year}}', coalesce(i.financial_year, '')), '{{release}}', coalesce(m.release_label, '')),
       m.licence_id, s.licence_name, s.licence_url, m.landing_page, m.asset_url, s.third_party_rights,
       m.retrieved_at, m.sha256
FROM used u
JOIN {ops}.asset_manifest m USING (source_id, asset_key, asset_version)
JOIN cfg_dim_source s ON s.source_id = m.source_id
LEFT JOIN cfg_stream_item i ON i.item_id = m.asset_key AND m.source_id = 'STREAM_APR'
"""


# --- Curated Gold views (stable definitions; they read the projected Gold views) ---------------

CURATED_VIEWS = {
    "vw_reported_performance": (
        """
SELECT f.company_code, c.company_name, c.service_type, f.company_code = d.company_code AS is_configured_company,
       f.reporting_area, f.financial_year, f.report_year, p.amp, p.price_review, f.metric_id, m.metric_name, m.theme,
       f.original_label, f.attribute, f.attribute_financial_year, f.rag_reference, f.source_item_reference, f.value,
       f.unit, f.price_base,
       m.direction, f.is_comparable, f.comparability_reason, f.is_preferred_source, f.restated,
       f.previous_release_value, f.source_id, s.publisher, s.licence_id, f.release_label, f.record_locator, f.sha256,
       f.observation_id
FROM {gold}.fact_reported_performance f
JOIN {gold}.dim_company c ON c.company_code = f.company_code
JOIN {gold}.dim_period p ON p.report_year = f.report_year
JOIN {gold}.dim_source s ON s.source_id = f.source_id
LEFT JOIN {gold}.dim_metric m ON m.metric_id = f.metric_id
CROSS JOIN {gold}.deployment_context d""",
        "Every published value with company, period, metric definition, comparability and source locator. "
        "One row per published value; report_year is the financial-year end (2024-25 = 2025).",
    ),
    "vw_peer_comparison": (
        """
WITH comparable AS (
  SELECT v.*, CASE WHEN m.service_scope = 'wastewater' THEN 'water_and_sewerage' ELSE 'all' END AS peer_group
  FROM {gold}.vw_reported_performance v JOIN {gold}.dim_metric m ON m.metric_id = v.metric_id
  WHERE v.is_comparable AND v.is_preferred_source
)
SELECT company_code, company_name, service_type, is_configured_company, financial_year, report_year, amp,
       metric_id, metric_name, theme, value, unit, price_base, direction, peer_group,
       CASE direction WHEN 'lower_is_better' THEN rank() OVER (PARTITION BY metric_id, report_year ORDER BY value ASC)
                      WHEN 'higher_is_better' THEN rank() OVER (PARTITION BY metric_id, report_year ORDER BY value DESC)
       END AS rank_in_peer_group,
       count(*) OVER (PARTITION BY metric_id, report_year) AS companies_in_peer_group,
       percentile_approx(value, 0.5) OVER (PARTITION BY metric_id, report_year) AS peer_median,
       source_id, release_label, record_locator, observation_id
FROM comparable""",
        "Comparable whole-company values only, ranked within each metric and year (rank 1 = best given the "
        "metric's direction). Wastewater metrics compare water and sewerage companies only.",
    ),
    "vw_commitment_vs_actual": (
        """
SELECT f.company_code, c.company_name, c.service_type, f.company_code = d.company_code AS is_configured_company,
       f.price_review, f.amp, f.financial_year, f.report_year, f.pc_id, f.pc_name, f.pc_type, f.primary_category,
       f.comparable_pc, f.unit, f.unit_description, f.direction, f.commitment_level, f.actual_performance,
       f.actual_performance - f.commitment_level AS actual_minus_commitment, f.pcl_met, f.baseline_level,
       f.enhanced_threshold, f.standard_collar, f.standard_cap, f.underperformance_deadband,
       f.outperformance_deadband, f.setting_level, f.source_id, f.release_label, f.record_locator, f.sha256,
       f.commitment_id
FROM {gold}.fact_performance_commitment f
JOIN {gold}.dim_company c ON c.company_code = f.company_code
CROSS JOIN {gold}.deployment_context d""",
        "Performance commitment levels (targets) and actual performance by company, commitment and year. PR19 = "
        "AMP7 (2020-25) with actuals and PCL met; PR24 = AMP8 (2025-30) levels only. Never mix the two.",
    ),
    "vw_odi_position": (
        """
SELECT f.company_code, c.company_name, f.company_code = d.company_code AS is_configured_company, f.price_review,
       f.financial_year, f.report_year, f.level, f.pc_id, f.pc_name, f.price_control, f.item_reference,
       f.payment_gbp_m, f.price_base, f.sharing_basis, f.source_id, f.release_label, f.record_locator, f.sha256,
       f.odi_id
FROM {gold}.fact_odi f
JOIN {gold}.dim_company c ON c.company_code = f.company_code
CROSS JOIN {gold}.deployment_context d""",
        "PR19 outcome delivery incentive payments in GBP million, 2017-18 prices. level = performance_commitment "
        "(before aggregate sharing) or price_control (net, after aggregate sharing). Never add the two levels.",
    ),
    "vw_source_coverage": (
        """
WITH facts AS (
  SELECT source_id, company_code, report_year, financial_year FROM {gold}.fact_reported_performance
  UNION ALL SELECT source_id, company_code, report_year, financial_year FROM {gold}.fact_performance_commitment
  UNION ALL SELECT source_id, company_code, report_year, financial_year FROM {gold}.fact_odi
),
cov AS (
  SELECT source_id, min(financial_year) AS first_financial_year, max(financial_year) AS last_financial_year,
         count(DISTINCT company_code) AS companies, count(*) AS published_rows
  FROM facts GROUP BY source_id
)
SELECT s.source_id, s.title, s.priority, s.status, coalesce(cov.published_rows, 0) > 0 AS available_in_release,
       CASE WHEN coalesce(cov.published_rows, 0) > 0 THEN NULL
            WHEN s.status = 'hold' THEN s.hold_reason
            ELSE 'Conditional source: no licence-verified version is in the published release.' END AS hold_reason,
       s.publisher, s.jurisdiction, s.landing_page, s.licence_id, s.licence_name, s.reporting_basis,
       cov.first_financial_year, cov.last_financial_year, cov.companies, coalesce(cov.published_rows, 0) AS published_rows
FROM {gold}.dim_source s LEFT JOIN cov ON cov.source_id = s.source_id""",
        "Every registered public source: status (enabled, conditional, hold), why a source is not available, "
        "and the years and companies it contributes to the published release. Use it to answer 'do you have ...?'",
    ),
    "vw_provenance": (
        """
WITH facts AS (
  SELECT 'reported_performance' AS fact_type, observation_id AS fact_id, company_code, financial_year, metric_id,
         coalesce(original_label, metric_id) AS label, value, unit, source_id, asset_key, asset_version,
         release_label, record_locator, sha256
  FROM {gold}.fact_reported_performance
  UNION ALL
  SELECT 'performance_commitment', commitment_id, company_code, financial_year, CAST(NULL AS STRING), pc_name,
         coalesce(commitment_level, actual_performance), unit, source_id, asset_key, asset_version, release_label,
         record_locator, sha256
  FROM {gold}.fact_performance_commitment
  UNION ALL
  SELECT 'odi', odi_id, company_code, financial_year, CAST(NULL AS STRING), coalesce(pc_name, price_control),
         payment_gbp_m, 'GBP m', source_id, asset_key, asset_version, release_label, record_locator, sha256
  FROM {gold}.fact_odi
)
SELECT f.fact_type, f.fact_id, f.company_code, f.financial_year, f.metric_id, f.label AS original_label, f.value,
       f.unit, f.source_id, s.publisher, f.release_label, f.record_locator, f.sha256, m.api_item_id,
       m.asset_url, m.final_url, m.landing_page, m.retrieved_at, m.licence_id, s.licence_name, s.licence_url,
       m.licence_evidence, s.attribution, m.archive_path, m.byte_size, f.asset_version
FROM facts f
JOIN {ops}.asset_manifest m ON m.source_id = f.source_id AND m.asset_key = f.asset_key AND m.asset_version = f.asset_version
JOIN {gold}.dim_source s ON s.source_id = f.source_id""",
        "Where every Gold number came from: publisher, release, file or API record locator, SHA-256, asset and "
        "landing-page URLs, retrieval time and licence.",
    ),
}

METRIC_VIEW = """
CREATE OR REPLACE VIEW {gold}.mv_peer_comparison
WITH METRICS
LANGUAGE YAML
COMMENT 'Governed peer measures over comparable whole-company values (vw_peer_comparison).'
AS $$
version: 1.1
source: "{gold}.vw_peer_comparison"
comment: >-
  One row per company, comparable metric and financial year. Measures are only meaningful within one
  metric_id and one financial_year: never add values across metrics, units or companies.
dimensions:
  - name: company_code
    expr: company_code
  - name: company_name
    expr: company_name
  - name: is_configured_company
    expr: is_configured_company
    comment: True for the company this deployment is configured for (deployment_context).
  - name: metric_id
    expr: metric_id
  - name: metric_name
    expr: metric_name
  - name: theme
    expr: theme
  - name: financial_year
    expr: financial_year
    comment: Financial year label such as 2024-25.
  - name: report_year
    expr: report_year
    comment: Financial-year end (2024-25 = 2025).
  - name: unit
    expr: unit
  - name: direction
    expr: direction
  - name: peer_group
    expr: peer_group
measures:
  - name: company_value
    expr: MAX(value)
    comment: The value for one company, metric and year (there is exactly one row at that grain).
  - name: peer_median
    expr: percentile_approx(value, 0.5)
    comment: Median across the companies in the selection.
  - name: best_rank
    expr: MIN(rank_in_peer_group)
    comment: Rank within the peer group (1 = best for the metric's direction).
  - name: companies_compared
    expr: COUNT(DISTINCT company_code)
  - name: lowest_value
    expr: MIN(value)
  - name: highest_value
    expr: MAX(value)
$$
"""
