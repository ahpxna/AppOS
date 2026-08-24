#!/usr/bin/env python3
"""Deterministic, offline project-claim extractor for immutable repository snapshots.

The extractor deliberately emits narrow implementation observations rather than
marketing language.  Each claim points to one file/line and is safe to mark
stale independently when that evidence file changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
ANALYZER_VERSION = "repository_claims_v1_2026_08_24"
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php",
    ".tf", ".hcl", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".md", ".sql", ".sh",
}
IGNORE_PARTS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", "coverage"}
MAX_FILE_BYTES = 1_500_000

SECURITY_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("control:approval_review", "security_control", re.compile(r"\b(?:approval|human[_ -]?review|requires_human)\b", re.I)),
    ("control:sha256_integrity", "security_control", re.compile(r"\bsha[-_ ]?256\b|hashlib\.sha256", re.I)),
    ("control:reconciliation", "reliability_control", re.compile(r"needs_reconciliation|\breconcil(?:e|iation)", re.I)),
    ("control:fail_closed", "security_control", re.compile(r"fail[-_ ]?closed|permanenttaskerror|refus(?:e|es|ed)\b", re.I)),
    ("control:domain_allowlist", "security_control", re.compile(r"allowed_domains|domain[_ -]?allowlist|allowlisted? domain", re.I)),
    ("control:row_locking", "concurrency_control", re.compile(r"\bFOR\s+UPDATE\b", re.I)),
    ("control:ttl_expiry", "security_control", re.compile(r"token_expires_at|expires_at\s*[<=>]|\bTTL\b", re.I)),
)

TECH_FILE_HINTS: dict[str, tuple[str, ...]] = {
    "Python": ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"),
    "Node.js": ("package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"),
    "Go": ("go.mod",),
    "Rust": ("cargo.toml",),
    "Terraform": (".tf",),
    "Docker": ("dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"),
    "PostgreSQL": ("postgres", "psycopg"),
}

TECH_CONTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "FastAPI": re.compile(r"\bfastapi\b", re.I),
    "Flask": re.compile(r"\bflask\b", re.I),
    "Django": re.compile(r"\bdjango\b", re.I),
    "Playwright": re.compile(r"\bplaywright\b", re.I),
    "Selenium": re.compile(r"\bselenium\b", re.I),
    "Puppeteer": re.compile(r"\bpuppeteer\b", re.I),
    "React": re.compile(r"(?:\bfrom\s+['\"]react['\"]|\breact(?:-dom)?\b)", re.I),
    "Next.js": re.compile(r"(?:\bnext(?:/|['\"]|\s*[:=])|\bnextjs\b|\bnext\.js\b)", re.I),
    "Express": re.compile(r"\bexpress\b", re.I),
    "Redis": re.compile(r"\bredis\b", re.I),
    "Celery": re.compile(r"\bcelery\b", re.I),
    "SQLAlchemy": re.compile(r"\bsqlalchemy\b", re.I),
    "Pydantic": re.compile(r"\bpydantic\b", re.I),
}

# Content matches count as implementation evidence only in dependency/config or
# executable source, never from README prose alone.
TECH_CONTENT_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".toml", ".json", ".txt", ".cfg", ".ini"}
TECH_CONTENT_FILENAMES = {"requirements.txt", "pyproject.toml", "package.json", "setup.py", "setup.cfg"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_blob_sha(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def safe_text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def eligible_file(path: Path, repo: Path) -> bool:
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return False
    if any(part in IGNORE_PARTS for part in rel.parts):
        return False
    name = path.name.casefold()
    return path.is_file() and (path.suffix.casefold() in TEXT_SUFFIXES or name in {"dockerfile", "makefile", "procfile"})


def all_candidate_paths(repo: Path) -> list[Path]:
    return sorted(p for p in repo.rglob("*") if eligible_file(p, repo))


def resolve_candidate_paths(repo: Path, relative_paths: Iterable[str] | None) -> list[Path]:
    if relative_paths is None:
        return all_candidate_paths(repo)
    paths: list[Path] = []
    for raw in relative_paths:
        candidate = (repo / raw).resolve()
        if repo.resolve() not in candidate.parents and candidate != repo.resolve():
            continue
        if candidate.is_file() and eligible_file(candidate, repo):
            paths.append(candidate)
    return sorted(set(paths))


def _line_for(text: str, pattern: re.Pattern[str]) -> tuple[int | None, str | None]:
    for index, line in enumerate(text.splitlines(), 1):
        if pattern.search(line):
            return index, line.strip()[:600]
    return None, None


def _claim(*, key: str, kind: str, text: str, rel: str, blob_sha: str,
           line: int | None = None, excerpt: str | None = None,
           github_authority: float = 0.70, document_authority: float = 0.30,
           confidence: float = 0.75, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "claim_key": key,
        "claim_kind": kind,
        "claim_text": text,
        "evidence_path": rel,
        "evidence_blob_sha": blob_sha,
        "source_line_start": line,
        "source_line_end": line,
        "github_authority": github_authority,
        "document_authority": document_authority,
        "confidence": confidence,
        "metadata": {"excerpt": excerpt, "analyzer_version": ANALYZER_VERSION, **(metadata or {})},
    }


def _tech_claims(repo: Path, paths: list[Path]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        rel = path.relative_to(repo).as_posix()
        name = path.name.casefold()
        text = safe_text(path)
        blob = file_blob_sha(path)
        for tech, hints in TECH_FILE_HINTS.items():
            matched = False
            evidence_line = None
            excerpt = None
            for hint in hints:
                if hint.startswith(".") and path.suffix.casefold() == hint:
                    matched = True
                    break
                if hint in {"postgres", "psycopg"}:
                    pattern = re.compile(rf"\b{re.escape(hint)}\b", re.I)
                    evidence_line, excerpt = _line_for(text, pattern)
                    matched = evidence_line is not None
                elif name == hint:
                    matched = True
            key = f"tech:{tech.casefold().replace('.', '').replace(' ', '_')}"
            if matched and key not in seen:
                seen.add(key)
                claims.append(_claim(
                    key=key, kind="technology", text=f"Repository evidence shows use of {tech}.",
                    rel=rel, blob_sha=blob, line=evidence_line, excerpt=excerpt,
                    github_authority=0.80, document_authority=0.20, confidence=0.90,
                ))

        if path.suffix.casefold() in TECH_CONTENT_SUFFIXES or name in TECH_CONTENT_FILENAMES:
            for tech, pattern in TECH_CONTENT_PATTERNS.items():
                key = f"tech:{tech.casefold().replace('.', '').replace(' ', '_')}"
                if key in seen:
                    continue
                evidence_line, excerpt = _line_for(text, pattern)
                if evidence_line is None:
                    continue
                seen.add(key)
                claims.append(_claim(
                    key=key, kind="technology", text=f"Repository implementation references {tech}.",
                    rel=rel, blob_sha=blob, line=evidence_line, excerpt=excerpt,
                    github_authority=0.80, document_authority=0.20, confidence=0.88,
                    metadata={"technology": tech, "evidence_class": "dependency_or_source"},
                ))
    return claims


def extract_claims(repo: Path, relative_paths: Iterable[str] | None = None) -> list[dict[str, Any]]:
    repo = repo.resolve()
    paths = resolve_candidate_paths(repo, relative_paths)
    claims = _tech_claims(repo, paths)
    seen = {c["claim_key"] for c in claims}

    test_paths = [p for p in paths if "test" in p.name.casefold() or "tests" in p.relative_to(repo).parts]
    if test_paths:
        path = test_paths[0]
        rel = path.relative_to(repo).as_posix()
        claims.append(_claim(
            key="surface:automated_tests", kind="test_surface",
            text="Repository contains automated test source files.", rel=rel,
            blob_sha=file_blob_sha(path), github_authority=0.80, document_authority=0.20, confidence=0.95,
            metadata={"observed_test_file_count_in_scope": len(test_paths)},
        ))
        seen.add("surface:automated_tests")

    service_paths = [p for p in paths if "services" in p.relative_to(repo).parts]
    if len({p.relative_to(repo).parts[1] for p in service_paths if len(p.relative_to(repo).parts) > 1}) >= 2:
        path = service_paths[0]
        claims.append(_claim(
            key="architecture:multi_service_layout", kind="architecture",
            text="Repository organizes implementation across multiple service modules.",
            rel=path.relative_to(repo).as_posix(), blob_sha=file_blob_sha(path),
            github_authority=0.75, document_authority=0.25, confidence=0.85,
        ))
        seen.add("architecture:multi_service_layout")

    workflow_paths = [p for p in paths if ".github" in p.relative_to(repo).parts and "workflows" in p.relative_to(repo).parts]
    if workflow_paths:
        path = workflow_paths[0]
        claims.append(_claim(
            key="delivery:github_actions", kind="delivery",
            text="Repository contains GitHub Actions workflow configuration.",
            rel=path.relative_to(repo).as_posix(), blob_sha=file_blob_sha(path),
            github_authority=0.80, document_authority=0.20, confidence=0.95,
        ))
        seen.add("delivery:github_actions")

    for path in paths:
        text = safe_text(path)
        if not text:
            continue
        rel = path.relative_to(repo).as_posix()
        blob = file_blob_sha(path)
        for key, kind, pattern in SECURITY_PATTERNS:
            if key in seen:
                continue
            line, excerpt = _line_for(text, pattern)
            if line is None:
                continue
            descriptions = {
                "control:approval_review": "Repository implementation contains an explicit approval or human-review control.",
                "control:sha256_integrity": "Repository implementation uses SHA-256 integrity hashing.",
                "control:reconciliation": "Repository implementation contains explicit reconciliation handling for uncertain state.",
                "control:fail_closed": "Repository implementation contains an explicit fail-closed/refusal path.",
                "control:domain_allowlist": "Repository implementation contains domain allowlisting logic.",
                "control:row_locking": "Repository implementation uses database row locking with FOR UPDATE.",
                "control:ttl_expiry": "Repository implementation contains explicit expiry/TTL handling.",
            }
            claims.append(_claim(
                key=key, kind=kind, text=descriptions[key], rel=rel, blob_sha=blob,
                line=line, excerpt=excerpt, github_authority=0.80, document_authority=0.20, confidence=0.80,
            ))
            seen.add(key)
    return sorted(claims, key=lambda item: item["claim_key"])


def classify_changed_files(files: Iterable[dict[str, Any] | str]) -> dict[str, Any]:
    paths: list[str] = []
    statuses: dict[str, str] = {}
    for item in files:
        if isinstance(item, str):
            path, status = item, "modified"
        else:
            path = str(item.get("filename") or item.get("path") or "")
            status = str(item.get("status") or "modified")
            previous = str(item.get("previous_filename") or "")
            if previous:
                statuses[previous] = "renamed_from"
                paths.append(previous)
        if path:
            paths.append(path)
            statuses[path] = status

    buckets: dict[str, list[str]] = {
        "dependencies": [], "runtime": [], "security": [], "tests": [],
        "infrastructure": [], "documentation": [], "generated": [], "other": [],
    }
    for path in sorted(set(paths)):
        low = path.casefold()
        if any(part in low for part in ("node_modules/", "dist/", "build/", "__pycache__/", ".min.js")):
            bucket = "generated"
        elif Path(low).name in {"requirements.txt", "pyproject.toml", "package.json", "go.mod", "cargo.toml", "poetry.lock", "pnpm-lock.yaml"}:
            bucket = "dependencies"
        elif low.endswith((".tf", ".hcl")) or any(x in low for x in ("dockerfile", "docker-compose", "compose.yml", "k8s/", "kubernetes/")):
            bucket = "infrastructure"
        elif "test" in Path(low).name or "/tests/" in f"/{low}" or low.startswith("tests/"):
            bucket = "tests"
        elif low.endswith((".md", ".rst")) or low.startswith("docs/"):
            bucket = "documentation"
        elif any(x in low for x in ("auth", "security", "approval", "policy", "permission", "credential", "secret", "token")):
            bucket = "security"
        elif Path(low).suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".sql", ".sh"}:
            bucket = "runtime"
        else:
            bucket = "other"
        buckets[bucket].append(path)
    substantive = sorted(set(paths) - set(buckets["generated"]))
    requires = bool(
        buckets["dependencies"] or buckets["runtime"] or buckets["security"]
        or buckets["tests"] or buckets["infrastructure"] or buckets["other"]
    )
    return {"paths": sorted(set(paths)), "statuses": statuses, "buckets": buckets,
            "substantive_paths": substantive, "requires_analysis": requires}


def _load_db():
    import psycopg
    from psycopg.types.json import Jsonb
    sys.path.insert(0, str(ROOT))
    from services.common.config import database_dsn
    return psycopg, Jsonb, database_dsn


def persist_claims(*, repository_source_id: str, project_id: str, snapshot_id: str,
                   claims: list[dict[str, Any]], affected_paths: Iterable[str],
                   full_reanalysis: bool = False, apply: bool) -> dict[str, Any]:
    psycopg, Jsonb, database_dsn = _load_db()
    affected = sorted(set(affected_paths))
    observed_keys = {c["claim_key"] for c in claims}
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        if full_reanalysis:
            cur.execute(
                """
                UPDATE repository_claims
                SET freshness_status='affected'
                WHERE repository_source_id=%s AND project_id=%s
                  AND freshness_status IN ('fresh','revalidated');
                """,
                (repository_source_id, project_id),
            )
        elif affected:
            cur.execute(
                """
                UPDATE repository_claims
                SET freshness_status='affected'
                WHERE repository_source_id=%s AND project_id=%s AND evidence_path = ANY(%s)
                  AND freshness_status IN ('fresh','revalidated');
                """,
                (repository_source_id, project_id, affected),
            )
        for claim in claims:
            cur.execute(
                """
                INSERT INTO repository_claims
                  (repository_source_id, project_id, claim_key, claim_kind, claim_text,
                   current_snapshot_id, evidence_path, evidence_blob_sha, source_line_start, source_line_end,
                   github_authority, document_authority, confidence, freshness_status, last_seen_at, metadata_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'fresh',now(),%s)
                ON CONFLICT (repository_source_id, project_id, claim_key)
                DO UPDATE SET claim_kind=EXCLUDED.claim_kind, claim_text=EXCLUDED.claim_text,
                              current_snapshot_id=EXCLUDED.current_snapshot_id,
                              evidence_path=EXCLUDED.evidence_path, evidence_blob_sha=EXCLUDED.evidence_blob_sha,
                              source_line_start=EXCLUDED.source_line_start, source_line_end=EXCLUDED.source_line_end,
                              github_authority=EXCLUDED.github_authority,
                              document_authority=EXCLUDED.document_authority,
                              confidence=EXCLUDED.confidence, freshness_status='revalidated',
                              last_seen_at=now(), metadata_json=EXCLUDED.metadata_json
                RETURNING id::text;
                """,
                (repository_source_id, project_id, claim["claim_key"], claim["claim_kind"], claim["claim_text"],
                 snapshot_id, claim["evidence_path"], claim["evidence_blob_sha"], claim["source_line_start"],
                 claim["source_line_end"], claim["github_authority"], claim["document_authority"],
                 claim["confidence"], Jsonb(claim["metadata"])),
            )
            claim_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO repository_claim_observations
                  (repository_claim_id, repository_snapshot_id, claim_text, evidence_path,
                   evidence_blob_sha, source_line_start, source_line_end, observation_status, metadata_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'observed',%s)
                ON CONFLICT (repository_claim_id, repository_snapshot_id)
                DO UPDATE SET claim_text=EXCLUDED.claim_text, evidence_path=EXCLUDED.evidence_path,
                              evidence_blob_sha=EXCLUDED.evidence_blob_sha,
                              source_line_start=EXCLUDED.source_line_start, source_line_end=EXCLUDED.source_line_end,
                              observation_status='revalidated', metadata_json=EXCLUDED.metadata_json;
                """,
                (claim_id, snapshot_id, claim["claim_text"], claim["evidence_path"], claim["evidence_blob_sha"],
                 claim["source_line_start"], claim["source_line_end"], Jsonb(claim["metadata"])),
            )
        if full_reanalysis:
            cur.execute(
                """
                UPDATE repository_claims
                SET freshness_status='source_missing'
                WHERE repository_source_id=%s AND project_id=%s
                  AND freshness_status='affected'
                  AND NOT (claim_key = ANY(%s));
                """,
                (repository_source_id, project_id, sorted(observed_keys) or ["__none__"]),
            )
        elif affected:
            cur.execute(
                """
                UPDATE repository_claims
                SET freshness_status='source_missing'
                WHERE repository_source_id=%s AND project_id=%s
                  AND freshness_status='affected'
                  AND evidence_path = ANY(%s)
                  AND NOT (claim_key = ANY(%s));
                """,
                (repository_source_id, project_id, affected, sorted(observed_keys) or ["__none__"]),
            )
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {"claims_observed": len(claims), "affected_paths": affected,
            "full_reanalysis": full_reanalysis, "committed": apply}


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract narrow project implementation claims from an immutable local checkout.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    claims = extract_claims(args.repo, args.path or None)
    payload = {"analyzer_version": ANALYZER_VERSION, "repo": str(args.repo), "claims": claims}
    text = json.dumps(payload, indent=2)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
