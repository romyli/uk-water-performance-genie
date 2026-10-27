"""The source acceptance gate rejects incomplete or unlicensed registry entries."""

from __future__ import annotations

import copy
import shutil

import pytest
import yaml

from water_performance import registry


@pytest.fixture
def config_copy(artifact_dir):
    d = artifact_dir / "config"
    shutil.copytree(registry.CONFIG_DIR, d)
    return d


def _edit(config_dir, source_id, **changes):
    path = config_dir / "sources.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for s in doc["sources"]:
        if s["source_id"] == source_id:
            for k, v in changes.items():
                if v is None:
                    s.pop(k, None)
                else:
                    s[k] = v
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_shipped_registry_is_valid():
    reg = registry.load_registry()
    assert {"STREAM_APR", "OFWAT_WCPR"} <= {s.source_id for s in reg.enabled_sources()}


@pytest.mark.parametrize("field", ["landing_page", "attribution", "contract", "reporting_basis"])
def test_enabled_source_missing_a_gate_field_is_rejected(config_copy, field):
    _edit(config_copy, "OFWAT_WCPR", **{field: ""})
    with pytest.raises(registry.RegistryError, match=f"OFWAT_WCPR: {field} is not recorded"):
        registry.load_registry(config_copy)


def test_enabled_source_without_open_licence_is_rejected(config_copy):
    _edit(config_copy, "OFWAT_WCPR", licence={"id": "All rights reserved", "evidence": "x"})
    with pytest.raises(registry.RegistryError, match="not a recognised open licence"):
        registry.load_registry(config_copy)


def test_licence_evidence_is_required(config_copy):
    _edit(config_copy, "OFWAT_WCPR", licence={"id": "OGL-UK-3.0", "evidence": ""})
    with pytest.raises(registry.RegistryError, match="licence evidence is not recorded"):
        registry.load_registry(config_copy)


def test_hold_requires_a_reason(config_copy):
    _edit(config_copy, "CCW_COMPLAINTS", hold_reason=None)
    with pytest.raises(registry.RegistryError, match="hold sources must state hold_reason"):
        registry.load_registry(config_copy)


def test_unsupported_jurisdiction_cannot_be_enabled(config_copy):
    path = config_copy / "sources.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    wics = next(s for s in doc["sources"] if s["source_id"] == "WICS_RETURN")
    wics.update(
        status="enabled",
        licence={"id": "OGL-UK-3.0", "evidence": "x"},
        attribution="x",
        contract="x",
        contract_version=1,
        assets=[
            {"asset_id": "a", "release": "r", "url": "https://wics.scot/a.xlsx", "landing_page": "https://wics.scot/"}
        ],
    )
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="jurisdiction 'scotland' has no adapter"):
        registry.load_registry(config_copy)


def test_conditional_assets_need_a_verification_route(config_copy):
    path = config_copy / "sources.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    src = next(s for s in doc["sources"] if s["source_id"] == "OFWAT_PR24_OUTCOMES")
    src["assets"][0].pop("licence_verification")
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="conditional assets need licence_verification"):
        registry.load_registry(config_copy)


def test_stream_item_register_rules(config_copy):
    path = config_copy / "stream_apr_items.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    bad = copy.deepcopy(doc)
    bad["items"].append(copy.deepcopy(bad["items"][0]))
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="registered twice"):
        registry.load_registry(config_copy)
    bad = copy.deepcopy(doc)
    bad["items"][0]["status"] = "hold"
    bad["items"][0].pop("hold_reason", None)
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="hold needs hold_reason"):
        registry.load_registry(config_copy)


def test_held_items_are_the_unlicensed_ones():
    held = {i.item_id: i for i in registry.load_registry().stream_items if i.status == "hold"}
    assert set(held) == {"ccd2a005a25e457caa6b85b929ebe9c3", "0690221de5774c33bab091608c03463a"}
