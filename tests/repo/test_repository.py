"""Repository rules: source register parity, licence gates, no tracked data, no deployment values,
serverless-only bundles and documentation hygiene."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from water_performance import registry

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", ".venv", ".databricks", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
CONTROL_FILES = {"BUILD_PROMPT.md"}  # build-control file; never part of the release or the workspace sync
CUSTOMER_DOCS = [
    "README.md",
    "sources.md",
    "CONTRIBUTING.md",
    "config.example.yml",
    *map(str, sorted(p.relative_to(ROOT) for p in (ROOT / "docs").glob("*.md"))),
]
REG = registry.load_registry()


def repo_files():
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_file() and not (set(rel.parts) & SKIP_DIRS) and rel.name not in CONTROL_FILES:
            yield rel


# --- Source register ---------------------------------------------------------------------------


def _register_rows() -> dict[str, str]:
    text = (ROOT / "sources.md").read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in re.finditer(r"^\| `([A-Z0-9_]+)` \| (P\d|Hold) \|", text, re.MULTILINE)}


def test_machine_registry_matches_the_human_register():
    rows = _register_rows()
    assert set(rows) == set(REG.sources), f"sources.md vs sources.yml: {set(rows) ^ set(REG.sources)}"
    for sid, priority in rows.items():
        assert REG.sources[sid]["priority"] == priority, sid


def test_enabled_sources_pass_the_licence_gate():
    for s in REG.sources.values():
        if s.status in ("enabled", "conditional"):
            assert registry.gate_failures(s, REG.licences) == [], s.source_id
            lic = s["licence"]
            assert lic["id"] in REG.licences and lic["evidence"] and s["attribution"] and s["landing_page"]


def test_guaranteed_minimum_release_is_enabled_and_other_p0_sources_are_conditional_or_held():
    assert REG.source("OFWAT_WCPR").status == "enabled" and REG.source("STREAM_APR").status == "enabled"
    for sid in ("OFWAT_PR24_OUTCOMES", "OFWAT_PR19_ODI", "RAG_DEFINITIONS", "COMPANY_APR"):
        assert REG.source(sid).status in ("conditional", "hold"), sid


def test_p1_and_other_jurisdictions_are_held():
    for s in REG.sources.values():
        if s["priority"] in ("P1", "P2"):
            assert s.status == "hold" and s.get("hold_reason"), s.source_id
    assert REG.source("WICS_RETURN")["jurisdiction"] == "scotland"
    assert REG.source("NI_AIR")["jurisdiction"] == "northern_ireland"


def test_forbidden_private_inputs_are_not_enabled():
    text = (ROOT / "bundles/data/src/water_performance/config/sources.yml").read_text(encoding="utf-8").lower()
    assert "industry_datashare" not in text and "industry datashare" not in text.split("forbidden_inputs:")[0]
    for s in REG.sources.values():
        for a in s.assets:
            assert "/Users/" not in a.url and a.url.startswith("https://www.")


def test_stream_items_are_traceable():
    assert len(REG.stream_items) >= 40
    for i in REG.stream_items:
        assert i.company_code in registry.load_companies(), i.item_id
        assert re.fullmatch(r"20\d\d-\d\d", i.financial_year), i.item_id


# --- Repository contents -----------------------------------------------------------------------

DATA_EXT = (".xlsx", ".xlsm", ".xls", ".xlsb", ".csv", ".pdf", ".parquet", ".jsonl", ".gz", ".zip", ".ashx", ".dbc")
CONFIG_DIR = Path("bundles/data/src/water_performance/config")


def test_no_source_data_is_tracked():
    offenders = [
        str(p)
        for p in repo_files()
        if p.suffix.lower() in DATA_EXT and not (p.parent == CONFIG_DIR and p.suffix == ".csv")
    ]
    assert offenders == []


PRIVATE = [
    (r"adb-\d{6,}", "workspace host"),
    (r"azuredatabricks\.net", "workspace host"),
    (r"dbc-[0-9a-f]{4,}", "workspace host"),
    (r"[A-Za-z0-9._%+-]+@databricks\.com", "email address"),
    (r"/Users/[A-Za-z]", "developer path"),
    (r"(?i)\bfevm\b", "build environment"),
    (r"git-emu", "private repository path"),
    (r"\b[0-9a-f]{16}\b", "warehouse-like ID"),
    (r"dapi[0-9a-f]{20,}", "token"),
    (r"pypi-proxy|cloud\.databricks\.com", "internal package index"),
]
SELF = Path(__file__).resolve().relative_to(ROOT)


@pytest.mark.parametrize("pattern,label", PRIVATE)
def test_no_deployment_or_private_values_are_committed(pattern, label):
    rx = re.compile(pattern)
    hits = []
    for rel in repo_files():
        if rel == SELF or rel.suffix.lower() not in (
            ".md",
            ".py",
            ".yml",
            ".yaml",
            ".json",
            ".toml",
            ".txt",
            ".csv",
            ".cfg",
            ".sql",
        ):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for m in rx.finditer(text):
            if label == "warehouse-like ID" and _is_hash_context(text, m):
                continue
            hits.append(f"{rel}: {m.group(0)}")
    assert hits == [], f"{label} found: {hits[:10]}"


def _is_hash_context(text: str, m: re.Match) -> bool:
    # 32-hex Stream item IDs and 64-hex checksums contain 16-hex runs; only a standalone 16-hex token counts.
    start, end = m.start(), m.end()
    return (start > 0 and re.match(r"[0-9a-f]", text[start - 1])) or (
        end < len(text) and re.match(r"[0-9a-f]", text[end])
    )


def test_build_control_file_is_not_shipped():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "BUILD_PROMPT" not in gitignore  # excluded locally, never advertised in the release
    for rel in repo_files():
        assert rel.name != "BUILD_PROMPT.md"


# --- Bundles -----------------------------------------------------------------------------------

BUNDLE_FILES = sorted((ROOT / "bundles").glob("*/resources/*.yml"))
CLASSIC = {
    "new_cluster",
    "existing_cluster_id",
    "job_clusters",
    "instance_pool_id",
    "node_type_id",
    "cluster_id",
    "driver_node_type_id",
}


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


@pytest.mark.parametrize("path", BUNDLE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_resource_files_are_named_by_type_and_use_serverless_only(path):
    kind = path.name.split(".")[-2]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    plural = {"job": "jobs", "schema": "schemas", "volume": "volumes", "genie_space": "genie_spaces"}[kind]
    assert list(doc["resources"]) == [plural]
    assert not (set(_keys(doc)) & CLASSIC)
    for job in (doc["resources"].get("jobs") or {}).values():
        for t in job["tasks"]:
            assert t.get("environment_key"), t["task_key"]


def test_permission_levels_and_grants_are_valid():
    levels = {
        "jobs": {"CAN_VIEW", "CAN_MANAGE_RUN", "CAN_MANAGE"},
        "genie": {"CAN_VIEW", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"},
    }
    privileges = {
        "schemas": {"USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE", "READ_VOLUME", "WRITE_VOLUME"},
        "volumes": {"READ_VOLUME", "WRITE_VOLUME"},
    }
    data = yaml.safe_load((ROOT / "bundles/data/databricks.yml").read_text(encoding="utf-8"))
    prod = data["targets"]["prod"]
    assert {p["level"] for p in prod["permissions"]} <= levels["jobs"]
    assert prod["run_as"]["service_principal_name"] == "${var.production_run_as}"
    for kind in ("schemas", "volumes"):
        for res in prod["resources"][kind].values():
            for g in res["grants"]:
                assert set(g["privileges"]) <= privileges[kind]
                assert g["principal"].startswith("${var.")
    genie = yaml.safe_load((ROOT / "bundles/genie/databricks.yml").read_text(encoding="utf-8"))
    for space in genie["targets"]["prod"]["resources"]["genie_spaces"].values():
        assert {p["level"] for p in space["permissions"]} <= levels["genie"]


def test_bundles_name_no_environment():
    for name in ("bundles/data/databricks.yml", "bundles/genie/databricks.yml"):
        doc = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        for target in doc["targets"].values():
            ws = target.get("workspace") or {}
            assert "host" not in ws and "profile" not in ws, name
        assert "warehouse_id" not in str(doc.get("variables", {}).get("warehouse_name", ""))


# --- Documentation -----------------------------------------------------------------------------

BANNED = [
    "in today's",
    "it's important to note",
    "it is important to note",
    "delve",
    "leverage",
    "utilize",
    "utilise",
    "robust",
    "seamless",
    "cutting-edge",
    "unlock",
    "future-proof",
    "game-changer",
    "game changer",
    "furthermore",
    "moreover",
]


@pytest.mark.parametrize("doc", CUSTOMER_DOCS)
def test_docs_avoid_stock_phrases(doc):
    text = (ROOT / doc).read_text(encoding="utf-8").lower().replace("’", "'")
    found = [w for w in BANNED if re.search(rf"(?<![a-z]){re.escape(w)}", text)]
    assert found == []


@pytest.mark.parametrize("doc", CUSTOMER_DOCS)
def test_docs_do_not_mention_build_internals(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    for word in (
        "BUILD_PROMPT",
        "build prompt",
        "Claude",
        "ChatGPT",
        "generated by AI",
        "AI-generated",
        "reference repository",
        "internal review",
        "internal note",
    ):
        assert word.lower() not in text.lower(), (doc, word)


@pytest.mark.parametrize("doc", CUSTOMER_DOCS)
def test_local_links_resolve(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    base = (ROOT / doc).parent
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if re.match(r"[a-z]+://|mailto:", target):
            continue
        assert (base / target).exists(), f"{doc}: broken link {target}"


def _declared_variables() -> set[str]:
    names = set()
    for name in ("bundles/data/databricks.yml", "bundles/genie/databricks.yml"):
        names |= set(yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["variables"])
    return names


def test_command_variables_match_bundle_variables():
    declared = _declared_variables()
    for doc in CUSTOMER_DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        used = set(re.findall(r"--var[ =]\"?([a-z_]+)=", text)) | set(re.findall(r"BUNDLE_VAR_([a-z_]+)", text))
        assert used <= declared, f"{doc}: undeclared variables {sorted(used - declared)}"
    example = yaml.safe_load((ROOT / "config.example.yml").read_text(encoding="utf-8"))
    assert set(example) <= declared


def test_readme_source_ids_exist():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"`([A-Z][A-Z0-9]+_[A-Z0-9_]+)`", text)) - {"BUNDLE_VAR"}
    ids = {i for i in ids if not i.startswith("BUNDLE_VAR_")}
    rows = _register_rows()
    assert ids and ids <= set(REG.sources) and ids <= set(rows), ids - set(REG.sources)


def test_readme_sections_are_in_order():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = [h.strip() for h in re.findall(r"^## (.+)$", text, re.MULTILINE)]
    expected = [
        "What this project does",
        "What you can ask",
        "How it works",
        "Data sources",
        "Before you start",
        "Deploy to a new Databricks workspace",
        "Choosing your company",
        "Routine operations",
        "Tracing a number to its source",
        "Security, costs and limitations",
    ]
    assert [h for h in headings if h in expected] == expected
