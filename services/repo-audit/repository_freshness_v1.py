#!/usr/bin/env python3
"""GitHub HEAD freshness, diff-aware offline analysis, and project-asset reconciliation.

Network access is confined to metadata/checkout acquisition.  Source analysis
runs against an immutable checkout pinned to the observed commit SHA.  Existing
claims are invalidated only when their evidence path changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(THIS_DIR))

from services.common.project_registry import configured_github_projects, load_registry
from repository_claims_v1 import ANALYZER_VERSION, classify_changed_files, extract_claims, persist_claims

SNAPSHOT_ROOT = ROOT / "data/repository_snapshots"
REFRESH_VERSION = "repository_freshness_v1_2026_08_24"
ASSET_COMPILER_VERSION = "project_authority_reconciled_v1_2026_08_24"
DEFAULT_MAX_STALE_HOURS = 24


class RefreshError(RuntimeError):
    pass


def _headers(token: str | None) -> dict[str, str]:
    result = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jobos-project-freshness/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def github_json(url: str, token: str | None) -> Any:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RefreshError(f"GitHub HTTP {exc.code}: {body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RefreshError(f"GitHub unavailable: {exc.reason}") from exc


def github_repository_state(repo_full_name: str, branch: str, token: str | None) -> dict[str, Any]:
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo_full_name.split("/", 1))
    repo = github_json(f"https://api.github.com/repos/{encoded_repo}", token)
    actual_branch = branch or repo.get("default_branch") or "main"
    commit = github_json(
        f"https://api.github.com/repos/{encoded_repo}/commits/{urllib.parse.quote(actual_branch, safe='')}", token
    )
    sha = str(commit.get("sha") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise RefreshError(f"GitHub did not return a full commit SHA for {repo_full_name}@{actual_branch}.")
    commit_obj = commit.get("commit") or {}
    tree = commit_obj.get("tree") or {}
    pushed = repo.get("pushed_at")
    return {
        "repo_full_name": repo_full_name,
        "canonical_url": repo.get("html_url") or f"https://github.com/{repo_full_name}",
        "clone_url": repo.get("clone_url") or f"https://github.com/{repo_full_name}.git",
        "default_branch": actual_branch,
        "head_sha": sha.lower(),
        "tree_sha": str(tree.get("sha") or "") or None,
        "pushed_at": pushed,
        "private": bool(repo.get("private")),
        "fork": bool(repo.get("fork")),
        "archived": bool(repo.get("archived")),
        "description": repo.get("description"),
        "language": repo.get("language"),
        "topics": repo.get("topics") or [],
        "homepage": repo.get("homepage"),
        "metadata": {"repo": repo, "commit": {"sha": sha, "commit": commit_obj}},
    }


def github_compare(repo_full_name: str, base_sha: str, head_sha: str, token: str | None) -> list[dict[str, Any]]:
    if base_sha == head_sha:
        return []
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo_full_name.split("/", 1))
    data = github_json(f"https://api.github.com/repos/{encoded_repo}/compare/{base_sha}...{head_sha}", token)
    files = data.get("files") or []
    result: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        result.append({
            "filename": item.get("filename"),
            "status": item.get("status"),
            "previous_filename": item.get("previous_filename"),
            "additions": item.get("additions"),
            "deletions": item.get("deletions"),
            "changes": item.get("changes"),
        })
    return result


def github_change_set(repo_full_name: str, base_sha: str | None, head_sha: str, token: str | None) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Return changed files plus whether correctness requires a full reanalysis.

    GitHub compare can fail after history rewriting and its file list is bounded.
    In either case the safe fallback is a full immutable-snapshot scan, not a
    stale last-known diff.
    """
    if not base_sha:
        return [], True, "first_snapshot"
    try:
        files = github_compare(repo_full_name, base_sha, head_sha, token)
    except RefreshError as exc:
        return [], True, f"compare_unavailable:{exc}"
    if len(files) >= 300:
        return files, True, "compare_file_limit_reached"
    return files, False, None


def _requires_material_analysis(*, prior_sha: str | None, full_reanalysis: bool,
                                classification: dict[str, Any]) -> bool:
    """Return whether this new snapshot can change resume-relevant repository facts."""
    if not prior_sha or full_reanalysis:
        return True
    return bool(classification.get("requires_analysis"))


def _safe_repo_name(repo_full_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", repo_full_name)


def snapshot_checkout_path(repo_full_name: str, head_sha: str) -> Path:
    return SNAPSHOT_ROOT / _safe_repo_name(repo_full_name) / head_sha / "source"


def _git_env(token: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    if token:
        # Keep the token out of argv/process listings.
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
        })
    return env


def ensure_immutable_checkout(*, repo_full_name: str, clone_url: str, head_sha: str, token: str | None) -> Path:
    destination = snapshot_checkout_path(repo_full_name, head_sha)
    marker = destination.parent / "snapshot.json"
    if destination.is_dir() and marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if payload.get("head_sha") == head_sha:
                return destination
        except Exception:
            pass
    if shutil.which("git") is None:
        raise RefreshError("git is required to materialize an immutable repository snapshot.")
    if destination.parent.exists():
        shutil.rmtree(destination.parent)
    destination.mkdir(parents=True, exist_ok=True)
    env = _git_env(token)
    commands = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", clone_url],
        ["git", "fetch", "-q", "--depth", "1", "origin", head_sha],
        ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
    ]
    for command in commands:
        proc = subprocess.run(command, cwd=destination, env=env, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            shutil.rmtree(destination.parent, ignore_errors=True)
            raise RefreshError(f"git snapshot fetch failed: {(proc.stderr or proc.stdout)[-800:]}")
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=destination, capture_output=True, text=True, timeout=15)
    if actual.returncode != 0 or actual.stdout.strip().lower() != head_sha.lower():
        shutil.rmtree(destination.parent, ignore_errors=True)
        raise RefreshError("Immutable checkout HEAD did not match the GitHub-observed SHA.")
    marker.write_text(json.dumps({
        "repo_full_name": repo_full_name, "head_sha": head_sha,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "fetcher_version": REFRESH_VERSION,
    }, indent=2) + "\n", encoding="utf-8")
    return destination


def _load_db():
    import psycopg
    from psycopg.types.json import Jsonb
    from services.common.config import database_dsn
    return psycopg, Jsonb, database_dsn


def _claim_material_hash(cur, source_id: str, project_id: str) -> str:
    cur.execute(
        """
        SELECT claim_key, claim_text
        FROM repository_claims
        WHERE repository_source_id=%s AND project_id=%s
          AND freshness_status IN ('fresh','revalidated')
        ORDER BY claim_key;
        """,
        (source_id, project_id),
    )
    payload = json.dumps(cur.fetchall(), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _upsert_source(cur, Jsonb, state: dict[str, Any]) -> str:
    cur.execute(
        """
        INSERT INTO repository_evidence_sources
          (provider, repo_full_name, canonical_url, clone_url, default_branch,
           revision_sha, is_private, is_fork, archived, description, homepage,
           primary_language, topics, source_payload, last_seen_at, updated_at,
           last_refresh_attempt_at, last_refresh_error)
        VALUES ('github',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now(),now(),NULL)
        ON CONFLICT (provider, repo_full_name)
        DO UPDATE SET canonical_url=EXCLUDED.canonical_url, clone_url=EXCLUDED.clone_url,
                      default_branch=EXCLUDED.default_branch, revision_sha=EXCLUDED.revision_sha,
                      is_private=EXCLUDED.is_private, is_fork=EXCLUDED.is_fork, archived=EXCLUDED.archived,
                      description=EXCLUDED.description, homepage=EXCLUDED.homepage,
                      primary_language=EXCLUDED.primary_language, topics=EXCLUDED.topics,
                      source_payload=EXCLUDED.source_payload, last_seen_at=now(), updated_at=now(),
                      last_refresh_attempt_at=now(), last_refresh_error=NULL
        RETURNING id::text;
        """,
        (state["repo_full_name"], state["canonical_url"], state["clone_url"], state["default_branch"],
         state["head_sha"], state["private"], state["fork"], state["archived"], state["description"],
         state["homepage"], state["language"], state["topics"], Jsonb(state["metadata"])),
    )
    return cur.fetchone()[0]


def _insert_snapshot(cur, Jsonb, source_id: str, state: dict[str, Any], parent_sha: str | None) -> str:
    cur.execute(
        """
        INSERT INTO repository_snapshots
          (repository_source_id, branch, head_sha, tree_sha, parent_head_sha, github_pushed_at, metadata_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (repository_source_id, head_sha)
        DO UPDATE SET observed_at=now(), branch=EXCLUDED.branch, tree_sha=EXCLUDED.tree_sha,
                      github_pushed_at=EXCLUDED.github_pushed_at, metadata_json=EXCLUDED.metadata_json
        RETURNING id::text;
        """,
        (source_id, state["default_branch"], state["head_sha"], state["tree_sha"], parent_sha,
         state["pushed_at"], Jsonb(state["metadata"])),
    )
    snapshot_id = cur.fetchone()[0]
    cur.execute(
        "UPDATE repository_evidence_sources SET current_snapshot_id=%s, revision_sha=%s WHERE id=%s",
        (snapshot_id, state["head_sha"], source_id),
    )
    return snapshot_id


def _map_existing_project_assets(cur, registry: dict[str, Any]) -> int:
    from services.common.project_registry import map_parsed_profile_record
    cur.execute(
        """
        SELECT id::text, asset_title, project_tags, tool_tags, canonical_narrative, source_strategy
        FROM profile_assets
        WHERE asset_type='project_asset' AND project_id IS NULL;
        """
    )
    updated = 0
    for asset_id, title, project_tags, tool_tags, narrative, source_strategy in cur.fetchall():
        mapping = map_parsed_profile_record({
            "asset_title": title, "project_tags": project_tags or [], "tags": tool_tags or [], "text": narrative or ""
        }, registry)
        if mapping.get("project_id"):
            cur.execute(
                """
                UPDATE profile_assets
                SET project_id=%s,
                    source_authority_json = source_authority_json || %s::jsonb,
                    updated_at=now()
                WHERE id=%s;
                """,
                (mapping["project_id"], json.dumps({"document": 0.30, "github": 0.70,
                                                    "mapping": mapping["confidence"]}), asset_id),
            )
            updated += 1
    return updated


def _current_claims(cur, source_id: str, project_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id::text, claim_key, claim_kind, claim_text, evidence_path, evidence_blob_sha,
               source_line_start, source_line_end, github_authority, document_authority, confidence
        FROM repository_claims
        WHERE repository_source_id=%s AND project_id=%s
          AND freshness_status IN ('fresh','revalidated')
        ORDER BY claim_kind, claim_key;
        """,
        (source_id, project_id),
    )
    return [
        {"id": r[0], "claim_key": r[1], "claim_kind": r[2], "claim_text": r[3], "evidence_path": r[4],
         "evidence_blob_sha": r[5], "line_start": r[6], "line_end": r[7], "github_authority": float(r[8]),
         "document_authority": float(r[9]), "confidence": float(r[10])}
        for r in cur.fetchall()
    ]


def _document_context(cur, project_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id::text, asset_title, canonical_narrative, job_oriented_summary,
               resume_bullet_bank, tool_tags, confidence
        FROM profile_assets
        WHERE project_id=%s AND asset_type='project_asset' AND status='approved'
          AND source_strategy <> 'project_authority_reconciled_v1'
          AND freshness_status IN ('fresh','not_applicable')
        ORDER BY confidence DESC NULLS LAST, updated_at DESC;
        """,
        (project_id,),
    )
    return [
        {"id": r[0], "title": r[1], "canonical": r[2] or "", "summary": r[3] or "",
         "resume": r[4] or "", "tools": r[5] or [], "confidence": float(r[6] or 0.0)}
        for r in cur.fetchall()
    ]


TECH_CONFLICT_FAMILIES: dict[str, set[str]] = {
    "python_web_framework": {"fastapi", "flask", "django"},
    "browser_automation": {"playwright", "selenium", "puppeteer"},
    "javascript_web_framework": {"express", "nextjs", "next_js"},
}


def _normalise_tech(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _reconcile_known_tool_conflicts(cur, *, source_id: str, project_id: str) -> list[dict[str, Any]]:
    """Record deterministic implementation conflicts that source authority can resolve.

    This is intentionally narrow.  A recognised mutually-exclusive framework
    disagreement is resolved to current GitHub implementation evidence (70/30
    policy).  Ambiguous semantic disagreements remain outside this automatic
    resolver and may be inserted as open conflicts by a future/manual reviewer.
    """
    cur.execute(
        """SELECT id::text, claim_key, claim_text
             FROM repository_claims
            WHERE repository_source_id=%s AND project_id=%s
              AND claim_kind='technology'
              AND freshness_status IN ('fresh','revalidated')""",
        (source_id, project_id),
    )
    github_claims = {
        _normalise_tech(str(row[1]).split(":", 1)[-1]): {"id": row[0], "claim_key": row[1], "text": row[2]}
        for row in cur.fetchall()
    }
    cur.execute(
        """SELECT id::text, tool_tags
             FROM profile_assets
            WHERE project_id=%s AND asset_type='project_asset' AND status='approved'
              AND source_strategy<>'project_authority_reconciled_v1'""",
        (project_id,),
    )
    docs = [(row[0], {_normalise_tech(str(tool)) for tool in (row[1] or [])}) for row in cur.fetchall()]
    cur.execute(
        """UPDATE project_source_conflicts
              SET status='superseded', updated_at=now()
            WHERE project_id=%s AND resolution_note LIKE 'AUTO_TECH_AUTHORITY:%%' AND status<>'superseded'""",
        (project_id,),
    )
    created: list[dict[str, Any]] = []
    for family, raw_members in TECH_CONFLICT_FAMILIES.items():
        members = {_normalise_tech(member) for member in raw_members}
        github_members = members.intersection(github_claims)
        if not github_members:
            continue
        for asset_id, document_tools in docs:
            doc_members = members.intersection(document_tools)
            if not doc_members or doc_members == github_members:
                continue
            for github_member in sorted(github_members):
                claim = github_claims[github_member]
                cur.execute(
                    """
                    INSERT INTO project_source_conflicts
                      (project_id, claim_key, github_claim_id, document_asset_id, github_value,
                       document_value, resolution, status, resolution_note, resolved_by, resolved_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,'github','resolved',%s,'source_authority_policy',now(),now())
                    ON CONFLICT (project_id, claim_key, github_claim_id, document_asset_id)
                    DO UPDATE SET github_value=EXCLUDED.github_value, document_value=EXCLUDED.document_value,
                                  resolution='github', status='resolved', resolution_note=EXCLUDED.resolution_note,
                                  resolved_by='source_authority_policy', resolved_at=now(), updated_at=now()
                    """,
                    (project_id, f"technology_family:{family}", claim["id"], asset_id,
                     ", ".join(sorted(github_members)), ", ".join(sorted(doc_members)),
                     f"AUTO_TECH_AUTHORITY: current GitHub implementation wins for {family} (GitHub 0.70/document 0.30)."),
                )
                created.append({"family": family, "github": sorted(github_members), "document": sorted(doc_members),
                                "resolution": "github"})
                break
    return created


def _authority_material(project: dict[str, Any], claims: list[dict[str, Any]], docs: list[dict[str, Any]]) -> dict[str, Any]:
    github_lines = [claim["claim_text"] for claim in claims]
    technologies = sorted({
        claim["claim_key"].split(":", 1)[1].replace("_", " ").title()
        for claim in claims if claim["claim_kind"] == "technology"
    })
    project_summary = str(project.get("project_summary") or "").strip()
    # Documents are lower-authority for implementation but remain useful for
    # user-authored purpose/motivation.  Do not copy their free-form resume
    # bullet banks into the dynamic implementation asset.
    document_purpose = next((doc["summary"] for doc in docs if doc["summary"].strip()), "")
    purpose = project_summary or document_purpose or f"User-confirmed project {project['display_name']}."
    implementation = " ".join(github_lines) if github_lines else "No current implementation claim has been extracted yet."
    return {
        "title": project["display_name"],
        "canonical": f"{purpose} Current repository evidence: {implementation}",
        "summary": f"{purpose} Current implementation evidence is pinned to the analyzed GitHub snapshot.",
        "resume": "\n".join(f"- {line}" for line in github_lines),
        "cover": purpose,
        "tools": technologies,
        "rules": [
            "Do not claim implementation facts that are absent from the current GitHub-backed claim set.",
            "Do not convert repository ownership into sole-authorship, professional deployment, scale, users, or outcome claims.",
            "Project name, dates, GitHub link, GPA, education, certifications and identity are fixed/user-verified fields, not inferred from code.",
        ],
        "authority": project.get("dynamic_source_policy") or {
            "implementation": {"github": 0.70, "document": 0.30},
            "purpose_motivation": {"github": 0.40, "document": 0.60},
        },
    }


def _material_hash(material: dict[str, Any]) -> str:
    """Hash resume-relevant project material independently of repository HEAD.

    ``source_snapshot_hash`` already records the exact GitHub revision. Keeping
    HEAD out of this hash lets documentation/generated-only commits advance the
    snapshot without manufacturing a new human-review candidate when the actual
    resume material did not change.
    """
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _asset_version_decision(prior_status: str | None, prior_material_hash: str | None,
                            source_material_hash: str) -> str:
    """Choose asset lifecycle solely from resume-relevant material identity.

    Snapshot churn alone never creates a new review candidate.  A previously
    rejected/superseded candidate with the same material also stays terminal;
    it is not silently resurrected.
    """
    if prior_status is None:
        return "create_candidate"
    if prior_material_hash == source_material_hash:
        if prior_status == "approved":
            return "revalidate_approved"
        if prior_status in {"draft", "needs_review", "pending_review"}:
            return "keep_candidate"
        return "keep_terminal"
    return "create_candidate"


def _compile_or_revalidate_asset(cur, Jsonb, *, source_id: str, snapshot_id: str, head_sha: str,
                                 project: dict[str, Any], material_changed: bool) -> dict[str, Any]:
    project_id = project["project_id"]
    claims = _current_claims(cur, source_id, project_id)
    docs = _document_context(cur, project_id)
    material = _authority_material(project, claims, docs)
    source_material_hash = _material_hash(material)
    cur.execute(
        """
        SELECT pa.id::text, pa.status, pa.source_material_hash
        FROM repository_evidence_asset_links l
        JOIN profile_assets pa ON pa.id=l.profile_asset_id
        WHERE l.repository_source_id=%s
          AND pa.project_id=%s
          AND pa.source_strategy='project_authority_reconciled_v1'
        ORDER BY pa.created_at DESC LIMIT 1;
        """,
        (source_id, project_id),
    )
    prior = cur.fetchone()
    decision = _asset_version_decision(
        prior[1] if prior else None, prior[2] if prior else None, source_material_hash
    )
    if prior and decision in {"revalidate_approved", "keep_candidate"}:
        cur.execute(
            """
            UPDATE profile_assets
            SET source_snapshot_hash=%s, source_material_hash=%s, freshness_status='fresh', valid_until=NULL,
                source_authority_json=%s, updated_at=now()
            WHERE id=%s;
            """,
            (head_sha, source_material_hash, Jsonb(material["authority"]), prior[0]),
        )
        cur.execute(
            """
            UPDATE repository_evidence_asset_links
            SET repository_snapshot_id=%s
            WHERE repository_source_id=%s AND profile_asset_id=%s;
            """,
            (snapshot_id, source_id, prior[0]),
        )
        return {
            "profile_asset_id": prior[0], "status": prior[1],
            "action": "revalidated" if decision == "revalidate_approved" else "existing_candidate",
        }
    if prior and decision == "keep_terminal":
        return {"profile_asset_id": prior[0], "status": prior[1], "action": "existing_terminal"}

    if prior:
        cur.execute(
            """
            UPDATE profile_assets
            SET freshness_status='stale', valid_until=COALESCE(valid_until,now()),
                status=CASE WHEN status IN ('draft','needs_review','pending_review') THEN 'superseded' ELSE status END,
                updated_at=now()
            WHERE project_id=%s AND source_strategy='project_authority_reconciled_v1'
              AND freshness_status='fresh';
            """,
            (project_id,),
        )
    cur.execute(
        """
        SELECT pa.id::text, pa.status
        FROM repository_evidence_asset_links l
        JOIN profile_assets pa ON pa.id=l.profile_asset_id
        WHERE l.repository_source_id=%s AND l.repository_snapshot_id=%s
          AND pa.compiler_version=%s AND pa.project_id=%s
          AND pa.source_material_hash=%s
        ORDER BY pa.created_at DESC LIMIT 1;
        """,
        (source_id, snapshot_id, ASSET_COMPILER_VERSION, project_id, source_material_hash),
    )
    existing = cur.fetchone()
    if existing:
        return {"profile_asset_id": existing[0], "status": existing[1], "action": "existing_candidate"}

    cur.execute(
        """
        INSERT INTO profile_assets
          (asset_title, asset_type, abstraction_level, status, canonical_narrative,
           job_oriented_summary, resume_bullet_bank, cover_letter_positioning,
           tool_tags, project_tags, do_not_overclaim_rules, compiler_version,
           source_strategy, confidence, review_note, project_id, source_snapshot_hash, source_material_hash,
           freshness_status, source_authority_json)
        VALUES (%s,'project_asset','synthesized_profile_asset','needs_review',%s,%s,%s,%s,%s,%s,%s,
                %s,'project_authority_reconciled_v1',0.80,%s,%s,%s,%s,'fresh',%s)
        RETURNING id::text;
        """,
        (material["title"], material["canonical"], material["summary"], material["resume"], material["cover"],
         material["tools"], [project_id, project.get("github_repo_full_name")], material["rules"],
         ASSET_COMPILER_VERSION, "Current GitHub implementation evidence changed; review this new project asset.",
         project_id, head_sha, source_material_hash, Jsonb(material["authority"])),
    )
    asset_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO repository_evidence_asset_links
          (repository_source_id, profile_asset_id, compiler_version, repository_snapshot_id)
        VALUES (%s,%s,%s,%s);
        """,
        (source_id, asset_id, ASSET_COMPILER_VERSION, snapshot_id),
    )
    for rank, claim in enumerate(claims, 1):
        cur.execute(
            """
            INSERT INTO profile_asset_evidence_items
              (profile_asset_id, evidence_rank, evidence_type, section_title, evidence_text,
               source_file_name, source_path, page_hint)
            VALUES (%s,%s,'source_excerpt',%s,%s,%s,%s,%s)
            """,
            (asset_id, rank, claim["claim_kind"], claim["claim_text"], project.get("github_repo_full_name"),
             claim["evidence_path"], f"sha:{claim['evidence_blob_sha']} lines:{claim['line_start']}-{claim['line_end']}"),
        )
    return {"profile_asset_id": asset_id, "status": "needs_review", "action": "created_candidate"}


def refresh_project(project: dict[str, Any], *, apply: bool, token: str | None = None) -> dict[str, Any]:
    token = token if token is not None else os.getenv("GH_TOKEN")
    state = github_repository_state(project["github_repo_full_name"], project.get("github_default_branch") or "main", token)
    checkout: Path | None = None
    psycopg, Jsonb, database_dsn = _load_db()

    if not apply:
        prior_sha = None
        try:
            with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT s.head_sha FROM repository_evidence_sources rs
                       LEFT JOIN repository_snapshots s ON s.id=rs.last_analyzed_snapshot_id
                       WHERE rs.provider='github' AND rs.repo_full_name=%s""",
                    (state["repo_full_name"],),
                )
                row = cur.fetchone()
                prior_sha = row[0] if row else None
        except Exception:
            prior_sha = None
        files, full_reanalysis, compare_reason = github_change_set(
            state["repo_full_name"], prior_sha, state["head_sha"], token
        )
        classification = classify_changed_files(files)
        classification["full_reanalysis"] = full_reanalysis
        classification["full_reanalysis_reason"] = compare_reason
        checkout = ensure_immutable_checkout(repo_full_name=state["repo_full_name"], clone_url=state["clone_url"],
                                             head_sha=state["head_sha"], token=token)
        claims = extract_claims(checkout, None if full_reanalysis else classification["substantive_paths"])
        return {
            "project_id": project["project_id"], "repo": state["repo_full_name"], "head_sha": state["head_sha"],
            "previous_sha": prior_sha, "changed": prior_sha != state["head_sha"],
            "classification": classification, "claims_preview": claims, "committed": False,
        }
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        source_id = _upsert_source(cur, Jsonb, state)
        cur.execute("SELECT ownership_status FROM repository_evidence_sources WHERE id=%s", (source_id,))
        ownership_status = cur.fetchone()[0]
        cur.execute(
            """
            SELECT s.id::text, s.head_sha
            FROM repository_evidence_sources rs
            LEFT JOIN repository_snapshots s ON s.id=rs.last_analyzed_snapshot_id
            WHERE rs.id=%s;
            """,
            (source_id,),
        )
        prior_snapshot_id, prior_sha = cur.fetchone()
        snapshot_id = _insert_snapshot(cur, Jsonb, source_id, state, prior_sha)
        if prior_sha == state["head_sha"]:
            cur.execute(
                "UPDATE repository_snapshots SET analysis_status='unchanged', analyzed_at=COALESCE(analyzed_at,now()) WHERE id=%s",
                (snapshot_id,),
            )
            cur.execute(
                "UPDATE repository_evidence_sources SET freshness_status='fresh', last_refresh_error=NULL WHERE id=%s",
                (source_id,),
            )
            _map_existing_project_assets(cur, load_registry())
            conflicts = _reconcile_known_tool_conflicts(
                cur, source_id=source_id, project_id=project["project_id"]
            )
            asset = (
                _compile_or_revalidate_asset(cur, Jsonb, source_id=source_id, snapshot_id=snapshot_id,
                                             head_sha=state["head_sha"], project=project, material_changed=False)
                if ownership_status == "confirmed_by_user"
                else {"action": "blocked_unconfirmed_ownership", "status": "not_built"}
            )
            result = {"project_id": project["project_id"], "repo": state["repo_full_name"], "head_sha": state["head_sha"],
                      "changed": False, "analysis": "skipped_same_head", "asset": asset, "resolved_conflicts": conflicts}
            if apply:
                conn.commit()
            else:
                conn.rollback()
            return {**result, "committed": apply}

        files, full_reanalysis, compare_reason = github_change_set(
            state["repo_full_name"], prior_sha, state["head_sha"], token
        )
        classification = classify_changed_files(files)
        classification["full_reanalysis"] = full_reanalysis
        classification["full_reanalysis_reason"] = compare_reason
        cur.execute(
            """
            INSERT INTO repository_change_sets
              (repository_source_id, base_snapshot_id, head_snapshot_id, changed_files, change_classification, requires_analysis)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (repository_source_id, head_snapshot_id)
            DO UPDATE SET changed_files=EXCLUDED.changed_files,
                          change_classification=EXCLUDED.change_classification,
                          requires_analysis=EXCLUDED.requires_analysis;
            """,
            (source_id, prior_snapshot_id, snapshot_id, Jsonb(files), Jsonb(classification), classification["requires_analysis"] or not prior_sha),
        )

        # A docs/generated-only commit advances the immutable source snapshot
        # but cannot invalidate implementation claims.  Do not fetch/analyze
        # source code or create a fresh review candidate for this case.
        no_material_analysis = not _requires_material_analysis(
            prior_sha=prior_sha, full_reanalysis=full_reanalysis, classification=classification
        )
        if no_material_analysis:
            _map_existing_project_assets(cur, load_registry())
            conflicts = _reconcile_known_tool_conflicts(
                cur, source_id=source_id, project_id=project["project_id"]
            )
            asset = (
                _compile_or_revalidate_asset(
                    cur, Jsonb, source_id=source_id, snapshot_id=snapshot_id,
                    head_sha=state["head_sha"], project=project, material_changed=False
                )
                if ownership_status == "confirmed_by_user"
                else {"action": "blocked_unconfirmed_ownership", "status": "not_built"}
            )
            cur.execute(
                "UPDATE repository_snapshots SET analysis_status='unchanged', analysis_version=%s, analyzed_at=now() WHERE id=%s",
                (ANALYZER_VERSION, snapshot_id),
            )
            cur.execute(
                """
                UPDATE repository_evidence_sources
                SET last_analyzed_snapshot_id=%s, current_snapshot_id=%s,
                    freshness_status='fresh', last_refresh_error=NULL, updated_at=now()
                WHERE id=%s
                """,
                (snapshot_id, snapshot_id, source_id),
            )
            result = {
                "project_id": project["project_id"], "repo": state["repo_full_name"],
                "head_sha": state["head_sha"], "previous_sha": prior_sha, "changed": True,
                "classification": classification, "analysis": "skipped_non_material_diff",
                "claims_observed": 0, "claim_material_changed": False, "asset": asset,
                "resolved_conflicts": conflicts,
            }
            if apply:
                conn.commit()
            else:
                conn.rollback()
            return {**result, "committed": apply}

        before_hash = _claim_material_hash(cur, source_id, project["project_id"])
        cur.execute("UPDATE repository_snapshots SET analysis_status='analyzing', analysis_version=%s WHERE id=%s", (ANALYZER_VERSION, snapshot_id))
        cur.execute("UPDATE repository_evidence_sources SET freshness_status='changed' WHERE id=%s", (source_id,))
        if apply:
            conn.commit()  # Persist observed snapshot before network checkout; failures remain diagnosable.
        else:
            conn.rollback()

    # Network acquisition happens outside a database transaction.
    checkout = ensure_immutable_checkout(repo_full_name=state["repo_full_name"], clone_url=state["clone_url"],
                                         head_sha=state["head_sha"], token=token)
    analyze_paths = None if full_reanalysis else classification["substantive_paths"]
    claims = extract_claims(checkout, analyze_paths)
    affected = [] if full_reanalysis else classification["paths"]
    persist_claims(repository_source_id=source_id, project_id=project["project_id"], snapshot_id=snapshot_id,
                   claims=claims, affected_paths=affected, full_reanalysis=full_reanalysis, apply=apply)

    # Finalize snapshot + asset after claim persistence.
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        after_hash = _claim_material_hash(cur, source_id, project["project_id"])
        material_changed = before_hash != after_hash
        _map_existing_project_assets(cur, load_registry())
        conflicts = _reconcile_known_tool_conflicts(
            cur, source_id=source_id, project_id=project["project_id"]
        )
        asset = (
            _compile_or_revalidate_asset(cur, Jsonb, source_id=source_id, snapshot_id=snapshot_id,
                                         head_sha=state["head_sha"], project=project, material_changed=material_changed)
            if ownership_status == "confirmed_by_user"
            else {"action": "blocked_unconfirmed_ownership", "status": "not_built"}
        )
        cur.execute(
            "UPDATE repository_snapshots SET analysis_status='analyzed', analysis_version=%s, analyzed_at=now() WHERE id=%s",
            (ANALYZER_VERSION, snapshot_id),
        )
        cur.execute(
            """
            UPDATE repository_evidence_sources
            SET last_analyzed_snapshot_id=%s, current_snapshot_id=%s,
                freshness_status='fresh', last_refresh_error=NULL, updated_at=now()
            WHERE id=%s
            """,
            (snapshot_id, snapshot_id, source_id),
        )
        cur.execute("UPDATE profile_briefs SET is_stale=true WHERE is_stale=false")
        result = {
            "project_id": project["project_id"], "repo": state["repo_full_name"], "head_sha": state["head_sha"],
            "previous_sha": prior_sha, "changed": True, "classification": classification,
            "claims_observed": len(claims), "claim_material_changed": material_changed, "asset": asset,
            "resolved_conflicts": conflicts,
        }
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {**result, "committed": apply}


def record_refresh_error(repo_full_name: str, error: str) -> None:
    try:
        psycopg, _, database_dsn = _load_db()
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE repository_evidence_sources
                SET freshness_status='unavailable', last_refresh_attempt_at=now(),
                    last_refresh_error=%s, updated_at=now()
                WHERE provider='github' AND repo_full_name=%s;
                """,
                (error[:1000], repo_full_name),
            )
    except Exception:
        pass


def refresh_all(*, project_id: str | None = None, apply: bool = True, token: str | None = None) -> dict[str, Any]:
    projects = configured_github_projects()
    if project_id:
        projects = [project for project in projects if project["project_id"] == project_id]
    if not projects:
        return {"projects": [], "message": "No fixed project has github_repo_full_name configured.", "ok": True}
    results = []
    failures = []
    for project in projects:
        try:
            results.append(refresh_project(project, apply=apply, token=token))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            record_refresh_error(project["github_repo_full_name"], message)
            failures.append({"project_id": project["project_id"], "repo": project["github_repo_full_name"], "error": message})
    return {"projects": results, "failures": failures, "ok": not failures}


def freshness_status() -> dict[str, Any]:
    projects = configured_github_projects()
    if not projects:
        return {"configured_projects": 0, "projects": [], "projects_fresh": True}
    psycopg, _, database_dsn = _load_db()
    rows: dict[str, dict[str, Any]] = {}
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_full_name, freshness_status, revision_sha, last_seen_at,
                   last_refresh_attempt_at, last_refresh_error,
                   ownership_status
            FROM repository_evidence_sources WHERE provider='github';
            """
        )
        for row in cur.fetchall():
            rows[row[0]] = {"freshness_status": row[1], "head_sha": row[2], "last_seen_at": row[3],
                            "last_refresh_attempt_at": row[4], "last_refresh_error": row[5], "ownership_status": row[6]}
    output = []
    for project in projects:
        state = rows.get(project["github_repo_full_name"], {})
        output.append({"project_id": project["project_id"], "repo": project["github_repo_full_name"], **state})
    fresh = all(item.get("freshness_status") == "fresh" for item in output)
    return {"configured_projects": len(output), "projects": output, "projects_fresh": fresh}


def pre_resume_refresh(*, max_stale_hours: int = DEFAULT_MAX_STALE_HOURS) -> dict[str, Any]:
    result = refresh_all(apply=True)
    if result["ok"]:
        return result
    # Network failure may use a bounded last-known-good snapshot.  Anything
    # older than the configured horizon blocks generation.
    psycopg, _, database_dsn = _load_db()
    blocked = []
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        for failure in result["failures"]:
            cur.execute(
                """
                SELECT rs.last_analyzed_snapshot_id IS NOT NULL,
                       EXTRACT(EPOCH FROM (now() - s.analyzed_at))/3600.0
                FROM repository_evidence_sources rs
                LEFT JOIN repository_snapshots s ON s.id=rs.last_analyzed_snapshot_id
                WHERE rs.provider='github' AND rs.repo_full_name=%s;
                """,
                (failure["repo"],),
            )
            row = cur.fetchone()
            hours = float(row[1]) if row and row[1] is not None else None
            if not row or not row[0] or hours is None or hours > max_stale_hours:
                blocked.append({**failure, "last_known_good_age_hours": hours})
    result["last_known_good_policy_hours"] = max_stale_hours
    result["blocked"] = blocked
    result["ok"] = not blocked
    return result


def watch(*, interval_seconds: int, project_id: str | None = None) -> int:
    if interval_seconds < 3600:
        raise RefreshError("Watch interval must be at least 3600 seconds.")
    while True:
        payload = refresh_all(project_id=project_id, apply=True)
        print(json.dumps(payload, indent=2, default=str), flush=True)
        time.sleep(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh configured GitHub projects by immutable HEAD snapshot.")
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--project-id")
    refresh.add_argument("--dry-run", action="store_true")
    preflight = sub.add_parser("pre-resume")
    preflight.add_argument("--max-stale-hours", type=int, default=int(os.getenv("JOBOS_PROJECT_MAX_STALE_HOURS", DEFAULT_MAX_STALE_HOURS)))
    sub.add_parser("status")
    watcher = sub.add_parser("watch")
    watcher.add_argument("--project-id")
    watcher.add_argument("--interval-seconds", type=int, default=86400)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(freshness_status(), indent=2, default=str))
        return 0
    if args.command == "pre-resume":
        payload = pre_resume_refresh(max_stale_hours=args.max_stale_hours)
        print(json.dumps(payload, indent=2, default=str))
        return 0 if payload["ok"] else 2
    if args.command == "watch":
        return watch(interval_seconds=args.interval_seconds, project_id=args.project_id)
    payload = refresh_all(project_id=args.project_id, apply=not args.dry_run)
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
