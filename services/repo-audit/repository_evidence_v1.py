#!/usr/bin/env python3
"""Import reviewable GitHub-repository evidence without chunking source code.

Repository visibility, metadata, and a passing test do not prove who wrote the
code or whether it was production work. This tool therefore requires two human
gates: explicit ownership confirmation, then explicit asset approval. Only the
approved asset becomes visible to document generation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn

DSN = database_dsn()

IMPORT_VERSION = "repository_evidence_v1_2026_08_20"
ASSET_COMPILER_VERSION = "repository_evidence_asset_compiler_v1_2026_08_20"


def norm(value: Any) -> str:
    """Normalize display text before deduplication or database storage."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def unique_terms(values: Iterable[Any]) -> list[str]:
    """Keep ordered, case-insensitive unique labels from imported metadata."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = norm(value)
        key = term.casefold()
        if term and key not in seen:
            result.append(term)
            seen.add(key)
    return result


def inventory_records(payload: Any) -> list[dict[str, Any]]:
    """Validate the manifest shape emitted by repo_inventory_v1.py."""
    rows = payload.get("repos", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Inventory must be a JSON object with a repos list, or a JSON list.")
    records: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        full_name = norm(raw.get("full_name"))
        canonical_url = norm(raw.get("html_url"))
        if not full_name or not canonical_url.startswith(("https://", "http://")):
            continue
        records.append({
            "repo_full_name": full_name,
            "canonical_url": canonical_url,
            "clone_url": norm(raw.get("clone_url")) or None,
            "default_branch": norm(raw.get("default_branch")) or None,
            "revision_sha": norm(raw.get("revision_sha")) or None,
            "is_private": bool(raw.get("private", False)),
            "is_fork": bool(raw.get("fork", False)),
            "archived": bool(raw.get("archived", False)),
            "description": norm(raw.get("description")) or None,
            "homepage": norm(raw.get("homepage")) or None,
            "primary_language": norm(raw.get("language")) or None,
            "topics": unique_terms(raw.get("topics") or []),
            "payload": raw,
        })
    if not records:
        raise ValueError("Inventory has no repositories with full_name and a valid html_url.")
    return records


def evidence_rows_for_repository(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn repository metadata into narrowly worded, source-linked evidence."""
    rows = [{
        "type": "repository_metadata",
        "key": "repository",
        "text": record["description"] or f"Repository metadata identifies {record['repo_full_name']}.",
        "url": record["canonical_url"],
        "payload": {"full_name": record["repo_full_name"], "default_branch": record["default_branch"]},
    }]
    if record["primary_language"]:
        rows.append({
            "type": "primary_language",
            "key": record["primary_language"].casefold(),
            "text": f"GitHub metadata lists {record['primary_language']} as the primary language.",
            "url": record["canonical_url"],
            "payload": {"primary_language": record["primary_language"]},
        })
    for topic in record["topics"]:
        rows.append({
            "type": "topic",
            "key": topic.casefold(),
            "text": f"GitHub metadata lists repository topic: {topic}.",
            "url": record["canonical_url"],
            "payload": {"topic": topic},
        })
    return rows


def audit_evidence_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Record isolated worker outcomes without claiming source-code authorship."""
    checks = report.get("checks") or []
    if not isinstance(checks, list):
        raise ValueError("Audit report checks must be a list.")
    records = []
    for check in checks:
        if not isinstance(check, dict) or not norm(check.get("name")):
            continue
        name = norm(check["name"])
        exit_code = check.get("exit_code")
        outcome = "passed" if exit_code == 0 else "did not pass"
        records.append({
            "type": "audit_check",
            "key": name.casefold(),
            "text": f"Isolated audit check '{name}' {outcome} (exit code: {exit_code!r}).",
            "path": norm(report.get("path")) or None,
            "payload": {
                "command": check.get("command"),
                "exit_code": exit_code,
                "error": norm(check.get("error")) or None,
                "stdout_tail": norm(check.get("stdout"))[-4000:],
                "stderr_tail": norm(check.get("stderr"))[-4000:],
            },
        })
    if not records:
        raise ValueError("Audit report has no named checks.")
    return records


def upsert_evidence(cur, source_id: str, rows: list[dict[str, Any]]) -> None:
    """Idempotently store source evidence for one imported repository."""
    for row in rows:
        cur.execute(
            """
            INSERT INTO repository_evidence_items
              (repository_source_id, evidence_type, evidence_key, evidence_text,
               source_url, source_path, evidence_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repository_source_id, evidence_type, evidence_key)
            DO UPDATE SET evidence_text = EXCLUDED.evidence_text,
                          source_url = EXCLUDED.source_url,
                          source_path = EXCLUDED.source_path,
                          evidence_json = EXCLUDED.evidence_json,
                          status = 'active';
            """,
            (source_id, row["type"], row["key"], row["text"], row.get("url"),
             row.get("path"), Jsonb(row["payload"])),
        )


def import_inventory(cur, manifest_path: Path) -> list[dict[str, str]]:
    """Import a reviewed inventory and attach metadata evidence to each source."""
    records = inventory_records(json.loads(manifest_path.read_text(encoding="utf-8")))
    result = []
    for record in records:
        cur.execute(
            """
            INSERT INTO repository_evidence_sources
              (provider, repo_full_name, canonical_url, clone_url, default_branch,
               revision_sha, is_private, is_fork, archived, description, homepage,
               primary_language, topics, source_payload)
            VALUES ('github', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider, repo_full_name)
            DO UPDATE SET canonical_url = EXCLUDED.canonical_url,
                          clone_url = EXCLUDED.clone_url,
                          default_branch = EXCLUDED.default_branch,
                          revision_sha = EXCLUDED.revision_sha,
                          is_private = EXCLUDED.is_private,
                          is_fork = EXCLUDED.is_fork,
                          archived = EXCLUDED.archived,
                          description = EXCLUDED.description,
                          homepage = EXCLUDED.homepage,
                          primary_language = EXCLUDED.primary_language,
                          topics = EXCLUDED.topics,
                          source_payload = EXCLUDED.source_payload,
                          last_seen_at = now(), updated_at = now()
            RETURNING id::text;
            """,
            (record["repo_full_name"], record["canonical_url"], record["clone_url"],
             record["default_branch"], record["revision_sha"], record["is_private"],
             record["is_fork"], record["archived"], record["description"],
             record["homepage"], record["primary_language"], record["topics"],
             Jsonb(record["payload"])),
        )
        source_id = cur.fetchone()[0]
        upsert_evidence(cur, source_id, evidence_rows_for_repository(record))
        result.append({"repository_source_id": source_id, "repo_full_name": record["repo_full_name"]})
    return result


def source_row(cur, source_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT id::text, repo_full_name, canonical_url, description, primary_language,
               topics, ownership_status, status
        FROM repository_evidence_sources WHERE id = %s;
        """, (source_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Repository source not found: {source_id}")
    return {"id": row[0], "repo_full_name": row[1], "canonical_url": row[2],
            "description": row[3] or "", "primary_language": row[4] or "",
            "topics": row[5] or [], "ownership_status": row[6], "status": row[7]}


def confirm_ownership(cur, source_id: str, actor: str) -> dict[str, Any]:
    """Record user confirmation; it does not assert code authorship."""
    source = source_row(cur, source_id)
    cur.execute(
        """
        UPDATE repository_evidence_sources
        SET ownership_status = 'confirmed_by_user', status = 'ownership_confirmed',
            ownership_confirmed_at = now(), ownership_confirmed_by = %s, updated_at = now()
        WHERE id = %s;
        """, (actor, source_id),
    )
    upsert_evidence(cur, source_id, [{
        "type": "user_ownership_confirmation", "key": "ownership",
        "text": f"Ownership of repository {source['repo_full_name']} was explicitly confirmed by {actor}.",
        "url": source["canonical_url"], "payload": {"actor": actor, "confirmation": "ownership_only"},
    }])
    return source


def compile_asset_material(source: dict[str, Any], audit_check_count: int) -> dict[str, Any]:
    """Build conservative project wording that stays within metadata evidence."""
    tech = unique_terms([source["primary_language"], *source["topics"]])
    tech_text = ", ".join(tech) if tech else "no technology labels"
    description = source["description"] or "No repository description was supplied in the imported metadata."
    return {
        "title": source["repo_full_name"].rsplit("/", 1)[-1],
        "canonical_narrative": (
            f"User-confirmed repository source: {source['repo_full_name']}. "
            f"Repository metadata: {description} Primary metadata labels: {tech_text}."
        ),
        "summary": (
            f"Portfolio repository {source['repo_full_name']} with metadata labels: {tech_text}. "
            "Use only as a user-confirmed repository/project reference."
        ),
        "resume": (
            f"Presented {source['repo_full_name']} as a portfolio repository; metadata lists {tech_text}."
        ),
        "cover": (
            f"Can reference the user-confirmed portfolio repository {source['repo_full_name']} "
            "only within the metadata and audit evidence recorded for it."
        ),
        "tools": tech,
        "project_tags": [source["repo_full_name"]],
        "rules": [
            "Do not claim sole authorship, employment, production deployment, users, scale, metrics, or outcomes unless a separate approved evidence source proves them.",
            "Do not turn repository ownership confirmation into a claim about implementation responsibility or seniority.",
            "Describe this as a portfolio repository/project, not professional work, unless separately evidenced.",
            "Do not state that tests passed unless the isolated audit evidence explicitly records the relevant passing check.",
        ],
        "audit_check_count": audit_check_count,
    }


def build_asset(cur, source_id: str, role_families: list[str]) -> str:
    """Compile confirmed evidence into a ``needs_review`` profile project asset.

    A second explicit approval is still required before L6 can use the asset;
    GitHub import cannot become an automatic résumé claim.
    """
    source = source_row(cur, source_id)
    if source["ownership_status"] != "confirmed_by_user" or source["status"] != "ownership_confirmed":
        raise ValueError("Confirm repository ownership before compiling a reviewable profile asset.")
    cur.execute(
        """
        SELECT id::text, evidence_type, evidence_text, source_url, source_path, evidence_json
        FROM repository_evidence_items
        WHERE repository_source_id = %s AND status = 'active'
        ORDER BY evidence_type, evidence_key;
        """, (source_id,),
    )
    evidence = [{"id": row[0], "type": row[1], "text": row[2], "url": row[3],
                 "path": row[4], "json": row[5] or {}} for row in cur.fetchall()]
    if not evidence:
        raise ValueError("Repository source has no active evidence to compile.")
    material = compile_asset_material(
        source, sum(item["type"] == "audit_check" for item in evidence)
    )
    cur.execute(
        """
        SELECT realink.profile_asset_id::text
        FROM repository_evidence_asset_links realink
        JOIN profile_assets pa ON pa.id = realink.profile_asset_id
        WHERE realink.repository_source_id = %s
          AND pa.compiler_version = %s
        ORDER BY pa.created_at DESC LIMIT 1;
        """, (source_id, ASSET_COMPILER_VERSION),
    )
    existing = cur.fetchone()
    if existing:
        return existing[0]
    cur.execute(
        """
        INSERT INTO profile_assets
          (asset_title, asset_type, abstraction_level, status, canonical_narrative,
           job_oriented_summary, resume_bullet_bank, cover_letter_positioning,
           role_families, tool_tags, project_tags, do_not_overclaim_rules,
           compiler_version, source_strategy, confidence, review_note)
        VALUES (%s, 'project_asset', 'source_preserving_asset', 'needs_review', %s,
                %s, %s, %s, %s, %s, %s, %s, %s, 'repository_evidence_compilation',
                0.55, 'Repository metadata and audit evidence require explicit user approval.')
        RETURNING id::text;
        """,
        (material["title"], material["canonical_narrative"], material["summary"],
         material["resume"], material["cover"], unique_terms(role_families), material["tools"],
         material["project_tags"], material["rules"], ASSET_COMPILER_VERSION),
    )
    asset_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO repository_evidence_asset_links
             (repository_source_id, profile_asset_id, compiler_version)
           VALUES (%s, %s, %s);""",
        (source_id, asset_id, ASSET_COMPILER_VERSION),
    )
    for rank, item in enumerate(evidence, 1):
        cur.execute(
            """
            INSERT INTO profile_asset_evidence_items
              (profile_asset_id, repository_evidence_item_id, evidence_rank, evidence_type,
               section_title, evidence_text, source_file_name, source_path, page_hint)
            VALUES (%s, %s, %s, 'source_excerpt', %s, %s, %s, %s, %s);
            """,
            (asset_id, item["id"], rank, item["type"], item["text"],
             source["repo_full_name"], item["path"] or source["canonical_url"], item["url"]),
        )
    return asset_id


def approve_asset(cur, asset_id: str, actor: str) -> None:
    """Make one ownership-confirmed, reviewed repository asset eligible for L6."""
    cur.execute(
        """
        UPDATE profile_assets pa
        SET status = 'approved', review_note = %s, updated_at = now()
        WHERE pa.id = %s
          AND pa.status IN ('draft', 'needs_review', 'pending_review')
          AND EXISTS (
            SELECT 1
            FROM repository_evidence_asset_links realink
            JOIN repository_evidence_sources rs ON rs.id = realink.repository_source_id
            WHERE realink.profile_asset_id = pa.id
              AND rs.ownership_status = 'confirmed_by_user'
              AND rs.status = 'ownership_confirmed'
          );
        """, (f"Approved by {actor} after repository evidence review.", asset_id),
    )
    if cur.rowcount != 1:
        raise ValueError("Asset is not an eligible reviewed repository asset, or ownership is not confirmed.")


def print_review(cur, source_id: str | None) -> None:
    sql = "SELECT row_to_json(v) FROM v_repository_evidence_review v"
    params: tuple[Any, ...] = ()
    if source_id:
        sql += " WHERE v.repository_source_id = %s"
        params = (source_id,)
    sql += " ORDER BY v.last_seen_at DESC"
    cur.execute(sql, params)
    print(json.dumps([row[0] for row in cur.fetchall()], indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Repository evidence import and review gates")
    sub = parser.add_subparsers(dest="command", required=True)
    inventory = sub.add_parser("import-inventory", help="Import a reviewed repo_inventory_v1 manifest.")
    inventory.add_argument("--manifest", required=True, type=Path)
    audit = sub.add_parser("import-audit", help="Attach one isolated worker report to a repository source.")
    audit.add_argument("--source-id", required=True)
    audit.add_argument("--report", required=True, type=Path)
    confirm = sub.add_parser("confirm-ownership", help="Record the user's ownership confirmation.")
    confirm.add_argument("--source-id", required=True)
    confirm.add_argument("--actor", default="candidate")
    build = sub.add_parser("build-asset", help="Compile a reviewable project asset from confirmed repository evidence.")
    build.add_argument("--source-id", required=True)
    build.add_argument("--role-family", action="append", default=[])
    approve = sub.add_parser("approve-asset", help="Make a reviewed repository asset available to L6.")
    approve.add_argument("--asset-id", required=True)
    approve.add_argument("--actor", default="candidate")
    review = sub.add_parser("review", help="List repository evidence and linked assets.")
    review.add_argument("--source-id")
    for command in (inventory, audit, confirm, build, approve):
        command.add_argument("--apply", action="store_true", help="Commit this change; otherwise rollback after preview.")
    args = parser.parse_args()

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            try:
                if args.command == "review":
                    print_review(cur, args.source_id)
                    conn.rollback()
                    return 0
                if args.command == "import-inventory":
                    result: Any = import_inventory(cur, args.manifest)
                elif args.command == "import-audit":
                    source_row(cur, args.source_id)
                    report = json.loads(args.report.read_text(encoding="utf-8"))
                    upsert_evidence(cur, args.source_id, audit_evidence_rows(report))
                    result = {"repository_source_id": args.source_id, "audit_checks": len(report.get("checks") or [])}
                elif args.command == "confirm-ownership":
                    source = confirm_ownership(cur, args.source_id, args.actor)
                    result = {"repository_source_id": source["id"], "ownership_status": "confirmed_by_user"}
                elif args.command == "build-asset":
                    result = {"profile_asset_id": build_asset(cur, args.source_id, args.role_family),
                              "status": "needs_review"}
                else:
                    approve_asset(cur, args.asset_id, args.actor)
                    result = {"profile_asset_id": args.asset_id, "status": "approved"}
                if args.apply:
                    conn.commit()
                    result["committed"] = True
                else:
                    conn.rollback()
                    result["committed"] = False
                print(json.dumps(result, indent=2))
                return 0
            except Exception as exc:
                conn.rollback()
                print(f"ERROR: {exc}")
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
