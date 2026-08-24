"""Aggregate resume-profile freshness policy shared by onboarding and L6."""
from __future__ import annotations

from datetime import date
from typing import Any

from services.common.fixed_profile_policy import readiness_from_records
from services.common.project_registry import load_registry


def fixed_state_from_cursor(cur) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    cur.execute(
        """
        SELECT field_key, value_json, display_value, verification_status,
               show_on_resume, verified_by, verified_at, expires_at
        FROM candidate_fixed_fields ORDER BY field_key;
        """
    )
    fields = {
        row[0]: {
            "value": row[1], "display_value": row[2], "verification_status": row[3],
            "show_on_resume": row[4], "verified_by": row[5], "verified_at": row[6], "expires_at": row[7],
        }
        for row in cur.fetchall()
    }
    cur.execute(
        """
        SELECT id::text, name, issuer, certification_status, earned_at, expires_at,
               credential_id, credential_url, show_on_resume, verification_status
        FROM candidate_certifications ORDER BY lower(name), lower(coalesce(issuer,''));
        """
    )
    certifications = [
        {"id": r[0], "name": r[1], "issuer": r[2], "certification_status": r[3],
         "earned_at": r[4], "expires_at": r[5], "credential_id": r[6], "credential_url": r[7],
         "show_on_resume": r[8], "verification_status": r[9]}
        for r in cur.fetchall()
    ]
    return fields, certifications


def assess_resume_profile(cur, *, today: date | None = None) -> dict[str, Any]:
    fields, certifications = fixed_state_from_cursor(cur)
    fixed = readiness_from_records(fields, certifications, today=today)
    cur.execute(
        "SELECT count(*) FROM candidate_fixed_field_suggestions WHERE status='pending' AND conflicts_current=true"
    )
    pending_fixed_conflicts = int(cur.fetchone()[0])
    fixed["pending_document_conflicts"] = pending_fixed_conflicts
    if pending_fixed_conflicts:
        fixed["fixed_fields_ready"] = False
    registry = load_registry()
    required_projects = [p for p in registry["projects"] if p.get("dynamic_source_mode", "github_primary") == "github_primary"]
    missing_project_mappings = [p["project_id"] for p in required_projects if not p.get("github_repo_full_name")]

    configured = [p for p in required_projects if p.get("github_repo_full_name")]
    project_rows: dict[str, dict[str, Any]] = {}
    if configured:
        cur.execute(
            """
            SELECT repo_full_name, freshness_status, ownership_status,
                   current_snapshot_id::text, last_analyzed_snapshot_id::text,
                   revision_sha, last_refresh_error
            FROM repository_evidence_sources
            WHERE provider='github' AND repo_full_name = ANY(%s);
            """,
            ([p["github_repo_full_name"] for p in configured],),
        )
        for row in cur.fetchall():
            project_rows[row[0]] = {
                "freshness_status": row[1], "ownership_status": row[2],
                "current_snapshot_id": row[3], "last_analyzed_snapshot_id": row[4],
                "head_sha": row[5], "last_refresh_error": row[6],
            }

    missing_sources: list[str] = []
    stale_sources: list[str] = []
    unconfirmed_sources: list[str] = []
    for project in configured:
        repo = project["github_repo_full_name"]
        row = project_rows.get(repo)
        if not row:
            missing_sources.append(repo)
            continue
        if row["ownership_status"] != "confirmed_by_user":
            unconfirmed_sources.append(repo)
        if row["freshness_status"] != "fresh" or row["current_snapshot_id"] != row["last_analyzed_snapshot_id"]:
            stale_sources.append(repo)

    cur.execute("SELECT count(*) FROM repository_claims WHERE freshness_status IN ('affected','contradicted','source_missing')")
    stale_claims = int(cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM project_source_conflicts WHERE status='open'")
    open_conflicts = int(cur.fetchone()[0])
    configured_project_ids = [p["project_id"] for p in configured]
    approved_project_ids: set[str] = set()
    if configured_project_ids:
        cur.execute(
            """
            SELECT DISTINCT project_id FROM profile_assets
            WHERE project_id = ANY(%s)
              AND asset_type='project_asset'
              AND source_strategy='project_authority_reconciled_v1'
              AND status='approved' AND freshness_status='fresh';
            """,
            (configured_project_ids,),
        )
        approved_project_ids = {row[0] for row in cur.fetchall()}
    missing_approved_project_assets = sorted(set(configured_project_ids) - approved_project_ids)
    cur.execute(
        """
        SELECT count(*) FROM profile_assets
        WHERE asset_type='project_asset' AND status='approved'
          AND freshness_status NOT IN ('fresh','not_applicable');
        """
    )
    stale_assets = int(cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM profile_assets WHERE asset_type='project_asset' AND status IN ('needs_review','pending_review','draft')")
    pending_project_assets = int(cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM profile_briefs WHERE is_stale=false")
    fresh_briefs = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT count(*) FROM profile_context_packs
        WHERE application_id IS NULL AND message_thread_id IS NULL
          AND purpose='base_resume_generation';
        """
    )
    resume_base_pack = int(cur.fetchone()[0])

    projects_fresh = not (missing_project_mappings or missing_sources or stale_sources or stale_claims or open_conflicts or stale_assets or pending_project_assets or unconfirmed_sources or missing_approved_project_assets)
    context_current = bool(fresh_briefs and resume_base_pack)
    ready = bool(fixed["fixed_fields_ready"] and projects_fresh and context_current)
    return {
        "fixed_fields": fixed,
        "projects_fresh": projects_fresh,
        "missing_project_mappings": missing_project_mappings,
        "missing_repository_sources": missing_sources,
        "unconfirmed_repository_sources": unconfirmed_sources,
        "stale_repository_sources": stale_sources,
        "stale_repository_claims": stale_claims,
        "open_project_conflicts": open_conflicts,
        "stale_approved_project_assets": stale_assets,
        "pending_project_assets": pending_project_assets,
        "missing_approved_project_assets": missing_approved_project_assets,
        "context_current": context_current,
        "fresh_briefs": fresh_briefs,
        "resume_base_pack": resume_base_pack,
        "resume_profile_ready": ready,
    }


def explain_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    fixed = report.get("fixed_fields") or {}
    if fixed.get("missing_fields"):
        blockers.append("missing/unverified fixed fields: " + ", ".join(fixed["missing_fields"]))
    if fixed.get("conflicting_fields"):
        blockers.append("conflicting fixed fields: " + ", ".join(fixed["conflicting_fields"]))
    if fixed.get("stale_fields"):
        blockers.append("fixed fields need reconfirmation: " + ", ".join(fixed["stale_fields"]))
    if fixed.get("pending_document_conflicts"):
        blockers.append(f"{fixed['pending_document_conflicts']} official-document fixed-field suggestion(s) conflict with the verified value")
    if fixed.get("invalid_visible_certifications"):
        blockers.append("invalid visible certifications: " + ", ".join(fixed["invalid_visible_certifications"]))
    for key, label in (
        ("missing_project_mappings", "projects without GitHub mapping"),
        ("missing_repository_sources", "GitHub sources not imported"),
        ("unconfirmed_repository_sources", "repository ownership not confirmed"),
        ("stale_repository_sources", "stale repositories"),
    ):
        if report.get(key):
            blockers.append(f"{label}: " + ", ".join(report[key]))
    if report.get("stale_repository_claims"):
        blockers.append(f"{report['stale_repository_claims']} stale repository claim(s)")
    if report.get("open_project_conflicts"):
        blockers.append(f"{report['open_project_conflicts']} unresolved project source conflict(s)")
    if report.get("stale_approved_project_assets"):
        blockers.append(f"{report['stale_approved_project_assets']} stale approved project asset(s)")
    if report.get("pending_project_assets"):
        blockers.append(f"{report['pending_project_assets']} project asset(s) require review")
    if report.get("missing_approved_project_assets"):
        blockers.append("projects without an approved current authority asset: " + ", ".join(report["missing_approved_project_assets"]))
    if not report.get("context_current"):
        blockers.append("profile briefs/base resume context pack are stale or missing")
    return blockers
