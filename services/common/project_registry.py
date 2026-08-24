"""User-owned project source of truth and conservative parsed-asset mapping.

The registry is intentionally a local JSON file rather than an LLM prompt or a
database migration.  It stores the facts the candidate has personally verified
for each resume project: immutable template identity, aliases, skills, allowed
facts, boundaries and evidence locations.  Later ingestion stages can map a
parsed record to a project only through those aliases; uncertain records stay
unmapped for review.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = Path(
    os.getenv("JOBOS_PROJECT_REGISTRY_PATH", REPO_ROOT / "data/project-registry/project_profiles.json")
).expanduser()
SCHEMA_VERSION = 2

# These are the six project blocks already present in the approved Word
# template.  Their IDs and resume slots are immutable.  Adding a new project
# requires a separately prepared Word block, then an intentional code/catalog
# update; a JSON edit cannot silently make a new project resume-eligible.
FIXED_PROJECT_CATALOG: tuple[dict[str, Any], ...] = (
    {"project_id": "caroect_d", "display_name": "CAROECT-D", "resume_slot_start": 1,
     "asset_title_aliases": ["CAROECT-D", "CAROECT D"]},
    {"project_id": "cig_amf", "display_name": "CIG-AMF", "resume_slot_start": 3,
     "asset_title_aliases": ["CIG-AMF", "CIG AMF"]},
    {"project_id": "pki_sentinel", "display_name": "PKI Sentinel", "resume_slot_start": 5,
     "asset_title_aliases": ["PKI Sentinel", "PKI-Sentinel"]},
    {"project_id": "applyops", "display_name": "ApplyOps", "resume_slot_start": 7,
     "asset_title_aliases": ["ApplyOps", "Apply Ops"]},
    {"project_id": "enterprise_netsec_iac", "display_name": "Enterprise NetSec IaC", "resume_slot_start": 9,
     "asset_title_aliases": ["Enterprise NetSec", "NetSec IaC", "Network Security IaC"]},
    {"project_id": "optimixer", "display_name": "Optimixer", "resume_slot_start": 11,
     "asset_title_aliases": ["Optimixer"]},
)

_MUTABLE_LIST_FIELDS = (
    "asset_title_aliases", "technology_tags", "skill_tags", "jd_keyword_tags",
    "allowed_facts", "do_not_overclaim", "evidence_locations", "source_urls",
)
_MUTABLE_TEXT_FIELDS = (
    "project_summary", "scope_status", "template_title", "template_date", "template_github_url",
    "github_repo_full_name", "github_default_branch", "dynamic_source_mode",
)
_MUTABLE_DICT_FIELDS = ("dynamic_source_policy",)


class ProjectRegistryError(ValueError):
    """The registry is malformed or tries to alter a protected template map."""


def _clean_text(value: Any, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _clean_list(value: Any, limit: int = 120) -> list[str]:
    values = value if isinstance(value, list) else []
    clean = [_clean_text(item, limit) for item in values]
    return list(dict.fromkeys(item for item in clean if item))


def _base_project(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **deepcopy(dict(item)),
        "template_title": "",
        "template_date": "",
        "template_github_url": "",
        "project_summary": "",
        "scope_status": "academic_project",
        "technology_tags": [],
        "skill_tags": [],
        "jd_keyword_tags": [],
        "allowed_facts": [],
        "do_not_overclaim": [],
        "evidence_locations": [],
        "source_urls": [],
        "github_repo_full_name": "",
        "github_default_branch": "main",
        "dynamic_source_mode": "github_primary",
        "dynamic_source_policy": {
            "implementation": {"github": 0.70, "document": 0.30},
            "tests_runtime": {"github": 0.80, "document": 0.20},
            "purpose_motivation": {"github": 0.40, "document": 0.60},
            "ownership_identity": {"user": 1.00},
        },
    }


def empty_registry() -> dict[str, Any]:
    """Return the initial six-block registry without creating a file."""
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": "",
        "projects": [_base_project(item) for item in FIXED_PROJECT_CATALOG],
    }


def _normalise_registry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProjectRegistryError("Project registry must be a JSON object.")
    incoming = raw.get("projects")
    if incoming is None:
        incoming = []
    if not isinstance(incoming, list):
        raise ProjectRegistryError("Project registry field 'projects' must be a list.")
    by_id: dict[str, Mapping[str, Any]] = {}
    for project in incoming:
        if not isinstance(project, dict):
            raise ProjectRegistryError("Every project entry must be a JSON object.")
        project_id = project.get("project_id")
        if project_id in by_id:
            raise ProjectRegistryError(f"Duplicate project_id: {project_id}")
        by_id[str(project_id)] = project

    expected = {item["project_id"] for item in FIXED_PROJECT_CATALOG}
    unexpected = set(by_id) - expected
    if unexpected:
        raise ProjectRegistryError(
            "This template supports only fixed projects; unexpected project_id(s): " + ", ".join(sorted(unexpected))
        )

    result = empty_registry()
    for target in result["projects"]:
        source = by_id.get(target["project_id"], {})
        if source and source.get("resume_slot_start") not in (None, target["resume_slot_start"]):
            raise ProjectRegistryError(f"{target['project_id']} cannot change its fixed resume slots.")
        if source and source.get("display_name") not in (None, target["display_name"]):
            raise ProjectRegistryError(f"{target['project_id']} cannot rename its approved resume project block.")
        for field in _MUTABLE_TEXT_FIELDS:
            if field in source:
                target[field] = _clean_text(source[field])
        for field in _MUTABLE_LIST_FIELDS:
            if field in source:
                # Base aliases remain so a user cannot accidentally unmap a
                # project by replacing a custom alias list in the form.
                merged = target[field] + _clean_list(source[field]) if field == "asset_title_aliases" else _clean_list(source[field])
                target[field] = list(dict.fromkeys(merged))
        for field in _MUTABLE_DICT_FIELDS:
            if field in source:
                if not isinstance(source[field], dict):
                    raise ProjectRegistryError(f"{target['project_id']}.{field} must be an object.")
                target[field] = deepcopy(source[field])
        repo_name = target.get("github_repo_full_name", "")
        if repo_name and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_name):
            raise ProjectRegistryError(f"{target['project_id']}.github_repo_full_name must be owner/repository.")
        branch = target.get("github_default_branch", "main") or "main"
        if any(ch.isspace() for ch in branch) or branch.startswith("-"):
            raise ProjectRegistryError(f"{target['project_id']}.github_default_branch is invalid.")
        target["github_default_branch"] = branch
        mode = target.get("dynamic_source_mode") or "github_primary"
        if mode not in {"github_primary", "document_only"}:
            raise ProjectRegistryError(f"{target['project_id']}.dynamic_source_mode must be github_primary or document_only.")
        target["dynamic_source_mode"] = mode
    return result


def load_registry(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the local registry; return a six-project draft if absent."""
    destination = Path(path or DEFAULT_REGISTRY_PATH).expanduser()
    if not destination.is_file():
        return empty_registry()
    try:
        return _normalise_registry(json.loads(destination.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ProjectRegistryError(f"Cannot parse project registry {destination}: {exc}") from exc


def save_registry(registry: Mapping[str, Any], path: Path | None = None) -> Path:
    """Validate then atomically save only user-owned project data."""
    destination = Path(path or DEFAULT_REGISTRY_PATH).expanduser()
    clean = _normalise_registry(dict(registry))
    clean["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
        json.dump(clean, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(destination)
    return destination


def project_asset_terms_by_slot(registry: Mapping[str, Any] | None = None) -> dict[int, tuple[str, ...]]:
    """Return normalized aliases used to bind profile assets to resume slots."""
    clean = _normalise_registry(registry if registry is not None else load_registry())
    terms: dict[int, tuple[str, ...]] = {}
    for project in clean["projects"]:
        aliases = [project["display_name"], *project["asset_title_aliases"]]
        normalized = tuple(dict.fromkeys(_clean_text(alias).casefold() for alias in aliases if _clean_text(alias)))
        terms[int(project["resume_slot_start"])] = normalized
    return terms


def map_parsed_profile_record(record: Mapping[str, Any], registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Map one parsed/aggregated record conservatively, or leave it unmapped.

    `record` can be a profile asset, parsed document, repository summary, or a
    future LLM extraction.  Only its title/tags/text are inspected; no model is
    invoked.  Ties remain unresolved instead of attaching evidence to the
    wrong project.
    """
    clean = _normalise_registry(registry if registry is not None else load_registry())
    fragments: list[str] = []
    for key in ("asset_title", "title", "project_tags", "tags", "text", "summary", "source_path"):
        value = record.get(key)
        fragments.extend(value if isinstance(value, list) else [value])
    haystack = " ".join(_clean_text(value, 5000).casefold() for value in fragments if value)
    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for project in clean["projects"]:
        aliases = project_asset_terms_by_slot({"projects": [project]})[project["resume_slot_start"]]
        hits = [alias for alias in aliases if len(alias) >= 4 and alias in haystack]
        if hits:
            scored.append((max(map(len, hits)), project, hits))
    if not scored:
        return {"project_id": None, "resume_slot_start": None, "confidence": "unmapped", "matched_aliases": []}
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return {"project_id": None, "resume_slot_start": None, "confidence": "ambiguous", "matched_aliases": []}
    _, project, hits = scored[0]
    return {
        "project_id": project["project_id"],
        "resume_slot_start": project["resume_slot_start"],
        "confidence": "alias_match",
        "matched_aliases": hits,
    }


def map_parsed_records(records: Iterable[Mapping[str, Any]], registry: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Attach a non-destructive mapping object to each parsed record."""
    clean = _normalise_registry(registry if registry is not None else load_registry())
    return [{**dict(record), "jobos_project_mapping": map_parsed_profile_record(record, clean)} for record in records]


def configured_github_projects(registry: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return only fixed projects that have an explicit GitHub repository mapping."""
    clean = _normalise_registry(registry if registry is not None else load_registry())
    return [deepcopy(project) for project in clean["projects"] if project.get("github_repo_full_name")]


def set_project_github_source(project_id: str, repo_full_name: str, *, branch: str = "main",
                              path: Path | None = None) -> Path:
    """Persist one explicit project->GitHub mapping without changing fixed resume slots."""
    registry = load_registry(path)
    matched = False
    for project in registry["projects"]:
        if project["project_id"] == project_id:
            project["github_repo_full_name"] = _clean_text(repo_full_name, 200)
            project["github_default_branch"] = _clean_text(branch, 200) or "main"
            matched = True
            break
    if not matched:
        raise ProjectRegistryError(f"Unknown fixed project_id: {project_id}")
    return save_registry(registry, path)
