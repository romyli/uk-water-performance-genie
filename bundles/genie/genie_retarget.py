"""Retarget Unity Catalog names in a serialized Genie space for the bundle target being deployed.

The checked-in `.geniespace.json` names its Gold objects with neutral authoring names, for example
`uk_water_performance_genie.water_performance_gold.vw_reported_performance`. At validate/deploy
time this mutator rewrites every occurrence for the target's catalog and schema:

* fully qualified names, plain or backtick-quoted (`` `cat`.`schema`.`table` ``), anywhere in the
  JSON: data source identifiers, join specs, SQL snippets, example SQL, benchmark SQL and text;
* Volume paths (`/Volumes/<catalog>/<schema>/<volume>/...`).

Rules map a source prefix to a target prefix with the same number of parts
(`catalog.schema` -> `catalog.schema`, or `catalog.schema.object` -> `catalog.schema.object`).
The most specific rule wins and each name is rewritten once, so rules never chain. A name only
matches at an identifier boundary: `xuk_water...` or `a.uk_water...` are left alone.

After rewriting, the mutator fails if any authoring catalog name is still present, sorts data
sources as the Genie API requires, and returns the space inline with `file_path` cleared.

Only the Python standard library is used outside `retarget_genie_spaces`, which needs the
`databricks-bundles` package that the Databricks CLI loads from this bundle's `.venv`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

MAPPING_VARIABLE = "genie_table_mapping"

_PART = r"(?:`(?:[^`]|``)+`|[A-Za-z_][A-Za-z0-9_]*)"
# A dotted name of 2 or 3 parts that does not continue a longer chain on its left.
_NAME = re.compile(rf"(?<![\w.`])({_PART})\.({_PART})(?:\.({_PART}))?")
_VPART = r"[A-Za-z0-9_\-]+"
_VOLUME = re.compile(rf"(?<![\w/])/Volumes/({_VPART})/({_VPART})(?:/({_VPART}))?(?=[/\s'\"`),;]|$)")

Rules = dict[tuple[str, ...], tuple[str, ...]]


class RetargetError(ValueError):
    """The mapping is invalid or the rewritten space still names an authoring object."""


def _unquote(part: str) -> str:
    return part[1:-1].replace("``", "`") if part.startswith("`") else part


def _quote(part: str) -> str:
    return "`" + part.replace("`", "``") + "`"


def _split(name: str) -> tuple[str, ...]:
    parts = tuple(p.strip() for p in str(name).split("."))
    if not 2 <= len(parts) <= 3 or not all(re.fullmatch(r"[A-Za-z0-9_\-]+", p) for p in parts):
        raise RetargetError(f"rule names must be catalog.schema or catalog.schema.object, got {name!r}")
    return parts


def parse_rules(mapping: Mapping[str, str] | None) -> Rules:
    rules: Rules = {}
    for src, dst in (mapping or {}).items():
        a, b = _split(src), _split(dst)
        if len(a) != len(b):
            raise RetargetError(f"rule {src!r} -> {dst!r}: both sides need the same number of parts")
        rules[tuple(p.lower() for p in a)] = b
    return rules


def _map(parts: tuple[str, ...], rules: Rules) -> tuple[str, ...] | None:
    key = tuple(p.lower() for p in parts)
    for n in (3, 2):
        if len(key) >= n and (dst := rules.get(key[:n])) is not None:
            new = dst + tuple(parts[n:])
            return None if tuple(p.lower() for p in new) == key else new
    return None


def retarget_text(text: str, rules: Rules) -> str:
    """Rewrite every matching name and Volume path in one pass (names and paths cannot overlap)."""
    if not rules or not text:
        return text

    def name_sub(m: re.Match) -> str:
        raw = tuple(p for p in m.groups() if p is not None)
        new = _map(tuple(_unquote(p) for p in raw), rules)
        if new is None:
            return m.group(0)
        return ".".join(_quote(n) if r.startswith("`") else n for r, n in zip(raw, new))

    def volume_sub(m: re.Match) -> str:
        raw = tuple(p for p in m.groups() if p is not None)
        new = _map(raw, rules)
        return m.group(0) if new is None else "/Volumes/" + "/".join(new)

    return _VOLUME.sub(volume_sub, _NAME.sub(name_sub, text))


def _walk(node, fn):
    if isinstance(node, dict):
        return {k: _walk(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, fn) for v in node]
    if isinstance(node, str):
        return fn(node)
    return node


def sort_data_sources(space: dict) -> dict:
    sources = space.get("data_sources") or {}
    for key in ("tables", "metric_views"):
        if isinstance(sources.get(key), list):
            sources[key].sort(key=lambda i: i.get("identifier", ""))
    if isinstance(sources.get("volumes"), list):
        sources["volumes"].sort(key=lambda v: v.get("path", ""))
    return space


def stale_references(space: dict, rules: Rules) -> list[str]:
    """Source prefixes still present after a rewrite, for every rule that changes its prefix."""
    text = json.dumps(space, ensure_ascii=False)
    stale = []
    for src, dst in rules.items():
        if tuple(p.lower() for p in dst) == src:
            continue
        dotted = r"`?\.`?".join(re.escape(p) for p in src)
        name = re.compile(rf"(?<![\w.`])`?{dotted}`?(?![\w`])", re.IGNORECASE)
        path = re.compile(r"/Volumes/" + "/".join(re.escape(p) for p in src) + r"(?![\w\-])", re.IGNORECASE)
        stale += [m.group(0) for m in name.finditer(text)] + [m.group(0) for m in path.finditer(text)]
    return stale


def retarget_space(space: dict | str, mapping: Mapping[str, str] | None) -> dict:
    if isinstance(space, str):
        space = json.loads(space)
    rules = parse_rules(mapping)
    out = sort_data_sources(_walk(space, lambda s: retarget_text(s, rules)))
    stale = stale_references(out, rules)
    if stale:
        raise RetargetError(f"authoring names remain after retargeting: {sorted(set(stale))[:10]}")
    return out


def _read_space_file(file_path: str) -> dict:
    for candidate in (Path(file_path), Path("resources") / file_path, Path.cwd() / file_path):
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise RetargetError(f"cannot find the serialized space file {file_path!r}")


try:  # Only the Databricks CLI's mutator run needs databricks-bundles.
    from dataclasses import replace

    from databricks.bundles.core import Bundle, Variable, genie_space_mutator
    from databricks.bundles.genie_spaces import GenieSpace

    @genie_space_mutator
    def retarget_genie_spaces(bundle: Bundle, space: GenieSpace) -> GenieSpace:
        """Rewrite each Genie space with the bundle's `genie_table_mapping` variable."""
        if MAPPING_VARIABLE not in bundle.variables:
            raise RetargetError(f"define the complex variable {MAPPING_VARIABLE!r} in databricks.yml")
        mapping = bundle.resolve_variable(Variable(path=f"var.{MAPPING_VARIABLE}", type=dict[str, str])) or {}
        serialized = space.serialized_space
        if serialized is None and space.file_path:
            original = _read_space_file(str(space.file_path))
        elif serialized is None:
            return space
        else:
            original = json.loads(serialized) if isinstance(serialized, str) else serialized
        body = retarget_space(original, mapping)
        # file_path and inline serialized_space are mutually exclusive in the Genie resource.
        return replace(space, file_path=None, serialized_space=json.dumps(body, indent=2, ensure_ascii=False))
except ImportError:  # pragma: no cover - exercised only where databricks-bundles is missing
    pass
