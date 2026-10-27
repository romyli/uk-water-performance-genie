"""Source registry: load the machine-readable register and apply the source acceptance gate.

A source (or one of its assets) is ingested only when the gate passes. The gate never relaxes a
licence requirement: a missing licence, attribution, release identity or parser contract keeps
the source out of every published fact.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).with_name("config")

STATUSES = ("enabled", "conditional", "hold")
RETRIEVAL_MODES = ("api", "direct_asset", "manual_bootstrap")
JURISDICTIONS = ("england_wales", "england", "wales", "scotland", "northern_ireland")
# The first release supports one jurisdiction model. Others need their own adapters.
SUPPORTED_JURISDICTIONS = ("england_wales", "england", "wales")

REQUIRED_FOR_ENABLED = (
    "source_id",
    "publisher",
    "landing_page",
    "jurisdiction",
    "coverage",
    "format",
    "retrieval_mode",
    "licence",
    "attribution",
    "reporting_basis",
    "contract",
    "contract_version",
)
REQUIRED_FIELDS = (
    "source_id",
    "title",
    "priority",
    "status",
    "publisher",
    "landing_page",
    "jurisdiction",
    "coverage",
    "format",
    "retrieval_mode",
    "licence",
    "attribution",
    "third_party_rights",
    "reporting_basis",
    "contract",
    "contract_version",
    "notes",
)
ASSET_REQUIRED = ("asset_id", "release", "url", "landing_page")


class RegistryError(ValueError):
    """The registry breaks a structural rule; the refresh must not start."""


@dataclass(frozen=True)
class Asset:
    source_id: str
    asset_id: str
    release: str
    url: str
    landing_page: str
    status: str
    published: str | None = None
    report_year_to: int | None = None
    supersedes: str | None = None
    licence_verification: str | None = None  # None (source-level evidence) or in_asset_statement
    hold_reason: str | None = None
    company_code: str | None = None  # company workbooks: the company and year the asset reports
    financial_year: str | None = None


@dataclass(frozen=True)
class Source:
    source_id: str
    raw: dict
    assets: tuple[Asset, ...] = field(default=())

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)

    @property
    def status(self) -> str:
        return self.raw["status"]

    @property
    def licence_id(self) -> str | None:
        return (self.raw.get("licence") or {}).get("id")


@dataclass(frozen=True)
class StreamItem:
    item_id: str
    title: str
    company_code: str
    source_company_code: str
    financial_year: str
    reporting_area: str
    item_type: str
    status: str
    service_url: str | None = None
    layer_id: int | None = None
    hold_reason: str | None = None


@dataclass(frozen=True)
class Registry:
    contract_version: int
    licences: dict
    sources: dict[str, Source]
    forbidden_inputs: tuple[dict, ...]
    stream_items: tuple[StreamItem, ...]

    def source(self, source_id: str) -> Source:
        try:
            return self.sources[source_id]
        except KeyError:
            raise RegistryError(f"unknown source_id {source_id!r}; known: {sorted(self.sources)}") from None

    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources.values() if s.status in ("enabled", "conditional")]


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_registry(config_dir: Path | str | None = None) -> Registry:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    doc = _load_yaml(base / "sources.yml")
    items_doc = _load_yaml(base / "stream_apr_items.yml")
    licences = doc.get("licences") or {}
    sources: dict[str, Source] = {}
    for raw in doc.get("sources") or []:
        sid = raw.get("source_id")
        if not sid or sid in sources:
            raise RegistryError(f"missing or duplicate source_id: {sid!r}")
        assets = tuple(
            Asset(
                source_id=sid,
                asset_id=a["asset_id"],
                release=a["release"],
                url=a["url"],
                landing_page=a["landing_page"],
                status=a.get("status") or raw.get("status"),
                published=None if a.get("published") is None else str(a.get("published")),
                report_year_to=a.get("report_year_to"),
                supersedes=a.get("supersedes"),
                licence_verification=a.get("licence_verification"),
                hold_reason=a.get("hold_reason"),
                company_code=a.get("company_code"),
                financial_year=None if a.get("financial_year") is None else str(a.get("financial_year")),
            )
            for a in raw.get("assets") or []
        )
        sources[sid] = Source(sid, raw, assets)
    items = tuple(
        StreamItem(
            item_id=i["item_id"],
            title=i["title"],
            company_code=i["company_code"],
            source_company_code=i["source_company_code"],
            financial_year=str(i["financial_year"]),
            reporting_area=i["reporting_area"],
            item_type=i["item_type"],
            status=i["status"],
            service_url=i.get("service_url"),
            layer_id=i.get("layer_id"),
            hold_reason=i.get("hold_reason"),
        )
        for i in items_doc.get("items") or []
    )
    reg = Registry(doc.get("contract_version", 1), licences, sources, tuple(doc.get("forbidden_inputs") or ()), items)
    validate_registry(reg)
    return reg


def gate_failures(source: Source, licences: dict) -> list[str]:
    """Reasons why a source may not be enabled. Empty means the gate passes."""
    raw = source.raw
    problems = []
    for key in REQUIRED_FOR_ENABLED:
        if raw.get(key) in (None, "", {}):
            problems.append(f"{key} is not recorded")
    lic = raw.get("licence") or {}
    lic_id = lic.get("id")
    if lic_id not in licences:
        problems.append(f"licence {lic_id!r} is not a recognised open licence")
    elif (licences[lic_id] or {}).get("redistribution") != "allowed_with_attribution":
        problems.append(f"licence {lic_id!r} does not allow redistribution")
    if not lic.get("evidence"):
        problems.append("licence evidence is not recorded")
    if raw.get("retrieval_mode") not in RETRIEVAL_MODES:
        problems.append(f"retrieval_mode {raw.get('retrieval_mode')!r} is not one of {RETRIEVAL_MODES}")
    if raw.get("jurisdiction") not in SUPPORTED_JURISDICTIONS:
        problems.append(f"jurisdiction {raw.get('jurisdiction')!r} has no adapter in this release")
    if raw.get("retrieval_mode") == "direct_asset" and not source.assets:
        problems.append("direct_asset source has no registered assets")
    for a in source.assets:
        for key in ASSET_REQUIRED:
            if not getattr(a, key):
                problems.append(f"asset {a.asset_id}: {key} is not recorded")
    return problems


def validate_registry(reg: Registry) -> None:
    errors = []
    for s in reg.sources.values():
        missing = [k for k in REQUIRED_FIELDS if k not in s.raw]
        if missing:
            errors.append(f"{s.source_id}: fields missing from the register entry: {missing}")
        if s.status not in STATUSES:
            errors.append(f"{s.source_id}: status {s.status!r} not in {STATUSES}")
        if s.status == "hold" and not s.get("hold_reason"):
            errors.append(f"{s.source_id}: hold sources must state hold_reason")
        if s.raw.get("jurisdiction") not in JURISDICTIONS:
            errors.append(f"{s.source_id}: unknown jurisdiction {s.raw.get('jurisdiction')!r}")
        if s.status in ("enabled", "conditional"):
            errors += [f"{s.source_id}: {p}" for p in gate_failures(s, reg.licences)]
        for a in s.assets:
            if a.status not in STATUSES:
                errors.append(f"{s.source_id}/{a.asset_id}: status {a.status!r} not in {STATUSES}")
            if s.status == "conditional" and a.status == "conditional" and a.licence_verification is None:
                errors.append(f"{s.source_id}/{a.asset_id}: conditional assets need licence_verification")
            if a.status == "hold" and not a.hold_reason:
                errors.append(f"{s.source_id}/{a.asset_id}: assets on hold must state hold_reason")
            if a.licence_verification not in (None, "in_asset_statement", "operator_confirmed"):
                errors.append(f"{s.source_id}/{a.asset_id}: unknown licence_verification {a.licence_verification!r}")
    seen = set()
    for i in reg.stream_items:
        if i.item_id in seen:
            errors.append(f"stream item {i.item_id} is registered twice")
        seen.add(i.item_id)
        if not re.fullmatch(r"[0-9a-f]{32}", i.item_id):
            errors.append(f"stream item {i.item_id!r} is not an ArcGIS item ID")
        if i.status not in ("enabled", "hold"):
            errors.append(f"stream item {i.item_id}: status {i.status!r}")
        if i.status == "hold" and not i.hold_reason:
            errors.append(f"stream item {i.item_id}: hold needs hold_reason")
        if i.item_type == "feature_service" and (not i.service_url or i.layer_id is None):
            errors.append(f"stream item {i.item_id}: feature_service items need service_url and layer_id")
        if i.reporting_area not in REPORTING_AREAS:
            errors.append(f"stream item {i.item_id}: unknown reporting_area {i.reporting_area!r}")
    if errors:
        raise RegistryError("source registry is invalid:\n  " + "\n  ".join(errors))


# --- Company crosswalk -------------------------------------------------------------------------


def load_reporting_areas(config_dir: Path | str | None = None) -> dict[str, tuple[str, str]]:
    """Publisher code -> (canonical company code, reporting area) for regional or alternative codes."""
    base = Path(config_dir) if config_dir else CONFIG_DIR
    with (base / "reporting_areas.csv").open(encoding="utf-8", newline="") as f:
        return {
            r["publisher_code"].strip(): (r["company_code"].strip(), r["reporting_area"].strip())
            for r in csv.DictReader(f)
        }


REPORTING_AREAS = {"whole_company"} | {area for _, area in load_reporting_areas().values()}

SERVICE_TYPES = ("water_and_sewerage", "water_only")


@dataclass(frozen=True)
class Company:
    company_code: str
    company_name: str
    service_type: str
    jurisdiction: str
    aliases: tuple[str, ...]
    notes: str


class CompanyConfigError(ValueError):
    """The configured company is not in the governed crosswalk."""


def load_companies(config_dir: Path | str | None = None) -> dict[str, Company]:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    out: dict[str, Company] = {}
    with (base / "companies.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            code = row["company_code"].strip()
            if code in out:
                raise RegistryError(f"company {code} is listed twice in companies.csv")
            if row["service_type"] not in SERVICE_TYPES:
                raise RegistryError(f"company {code}: unknown service_type {row['service_type']!r}")
            out[code] = Company(
                company_code=code,
                company_name=row["company_name"].strip(),
                service_type=row["service_type"],
                jurisdiction=row["jurisdiction"].strip(),
                aliases=tuple(a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()),
                notes=(row.get("notes") or "").strip(),
            )
    return out


def resolve_company(code: str, companies: dict[str, Company]) -> Company:
    """Exact match on the canonical code only. A near match is an error, never a fallback."""
    wanted = (code or "").strip()
    if wanted in companies:
        return companies[wanted]
    accepted = ", ".join(sorted(companies))
    hint = ""
    lowered = wanted.lower()
    for c in companies.values():
        if lowered and (
            lowered == c.company_code.lower()
            or lowered in (a.lower() for a in c.aliases)
            or lowered == c.company_name.lower()
        ):
            hint = f" Did you mean {c.company_code} ({c.company_name})? Use the code exactly."
    raise CompanyConfigError(
        f"company_code {wanted!r} is not in the company crosswalk. Accepted codes: {accepted}.{hint}"
    )


def company_alias_index(companies: dict[str, Company]) -> dict[str, str]:
    """Lower-cased code, name and alias -> canonical code, for reading publisher labels."""
    index: dict[str, str] = {}
    for c in companies.values():
        for label in (c.company_code, c.company_name, *c.aliases):
            index[label.strip().lower()] = c.company_code
    return index
