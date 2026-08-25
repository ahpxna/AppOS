from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"


def sql(version: int) -> str:
    matches = sorted(MIGRATIONS.glob(f"{version:03d}_*.sql"))
    assert len(matches) == 1, f"expected exactly one migration {version:03d}, got {matches}"
    return matches[0].read_text(encoding="utf-8")


def test_migrations_064_through_069_exist_once_and_are_transaction_wrapped():
    for version in range(64, 70):
        text = sql(version)
        assert re.search(r"\bBEGIN\s*;", text, re.I)
        assert re.search(r"\bCOMMIT\s*;\s*$", text, re.I)


def test_064_versions_logical_documents_by_content_sha_not_timestamp():
    text = sql(64)
    assert "CREATE TABLE IF NOT EXISTS profile_source_documents" in text
    assert "CREATE TABLE IF NOT EXISTS profile_source_revisions" in text
    assert "content_sha256 text NOT NULL" in text
    assert "UNIQUE(source_document_id, content_sha256)" in text
    assert "embedded_created_at" in text and "filesystem_modified_at" in text
    assert "ADD COLUMN IF NOT EXISTS source_revision_id" in text


def test_065_keeps_document_suggestions_separate_from_canonical_fixed_fields():
    text = sql(65)
    assert "CREATE TABLE IF NOT EXISTS candidate_fixed_fields" in text
    assert "CREATE TABLE IF NOT EXISTS candidate_fixed_field_suggestions" in text
    assert "conflicts_current boolean" in text
    assert "CREATE TABLE IF NOT EXISTS candidate_certifications" in text
    assert "certification_status" in text


def test_066_pins_repository_source_to_immutable_head_snapshots():
    text = sql(66)
    assert "CREATE TABLE IF NOT EXISTS repository_snapshots" in text
    assert "head_sha text NOT NULL" in text
    assert "UNIQUE(repository_source_id, head_sha)" in text
    assert "CREATE TABLE IF NOT EXISTS repository_change_sets" in text
    assert "current_snapshot_id" in text and "last_analyzed_snapshot_id" in text


def test_067_claim_freshness_is_evidence_granular():
    text = sql(67)
    assert "CREATE TABLE IF NOT EXISTS repository_claims" in text
    assert "evidence_path text" in text
    assert "evidence_blob_sha text" in text
    assert "source_line_start integer" in text
    assert "source_line_end integer" in text
    assert "github_authority numeric NOT NULL DEFAULT 0.70" in text
    assert "document_authority numeric NOT NULL DEFAULT 0.30" in text
    assert "CREATE TABLE IF NOT EXISTS project_source_conflicts" in text


def test_068_preserves_existing_document_generation_view_column_contract():
    old = sql(34)
    new = sql(68)
    wanted = [
        "profile_asset_id", "asset_title", "asset_type", "role_families", "competency_tags",
        "tool_tags", "job_oriented_summary", "resume_bullet_bank", "cover_letter_positioning",
        "interview_story", "do_not_overclaim_rules", "confidence",
    ]

    def aliases(text: str) -> list[str]:
        body = text.split("CREATE OR REPLACE VIEW v_document_generation_source_assets AS", 1)[1].split("FROM profile_assets pa", 1)[0]
        found = []
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper() == "SELECT":
                continue
            match = re.search(r"\bAS\s+([a-z_][a-z0-9_]*)$", line, re.I)
            found.append(match.group(1) if match else line.split(".")[-1])
        return found

    assert aliases(old) == wanted
    assert aliases(new) == wanted
    assert "freshness_status IN ('fresh','not_applicable')" in new
    assert "OR pa.source_strategy = 'project_authority_reconciled_v1'" in new
    assert "OR NOT EXISTS" not in new


def test_069_adds_aggregate_resume_freshness_views():
    text = sql(69)
    assert "CREATE OR REPLACE VIEW v_project_freshness_summary" in text
    assert "CREATE OR REPLACE VIEW v_resume_profile_freshness_gate" in text
    assert "stale_repository_claims" in text
    assert "open_project_conflicts" in text
    assert "stale_approved_project_assets" in text


def test_jobos_doctor_requires_latest_071_migration():
    source = (ROOT / "scripts" / "jobos.py").read_text(encoding="utf-8")
    assert "071_human_approval_bus_and_privileged_actions.sql" in source
    assert "Migrations through 071" in source
    assert (ROOT / "db" / "migrations" / "070_profile_freshness_hardening.sql").is_file()
    assert "Migrations through 063" not in source


def test_070_document_only_assets_are_resume_eligible():
    text = (ROOT / "db" / "migrations" / "070_profile_freshness_hardening.sql").read_text(encoding="utf-8")
    assert "project_document_only_v1" in text
    assert "v_document_generation_source_assets" in text
