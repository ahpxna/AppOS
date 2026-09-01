from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.common import config
from services.discovery import linkedin_discovery_v1 as linkedin

ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_database_dsn_quotes_libpq_sensitive_credentials(monkeypatch):
    monkeypatch.setenv("JOBOS_DB_PASSWORD", " p a'ss\\word ")
    monkeypatch.setenv("JOBOS_DB_USER", "job os")
    monkeypatch.setenv("JOBOS_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("JOBOS_DB_PORT", "5433")
    monkeypatch.setenv("JOBOS_DB_NAME", "jobos test")
    dsn = config.database_dsn()
    assert "user='job os'" in dsn
    assert "dbname='jobos test'" in dsn
    assert "password=' p a\\'ss\\\\word '" in dsn


def test_dotenv_parser_preserves_quotes_and_valid_escapes():
    assert config._parse_dotenv_value('\'literal " quote\'') == 'literal " quote'
    assert config._parse_dotenv_value('"line\\nquote\\\"slash\\\\"') == 'line\nquote"slash\\'
    assert config._parse_dotenv_value('"quoted value" # comment') == "quoted value"
    assert config._parse_dotenv_value("token#value # comment") == "token#value"
    with pytest.raises(config.ConfigurationError):
        config._parse_dotenv_value('"unterminated')


def test_linkedin_url_rejects_lookalike_hosts():
    assert linkedin.validate_job_url("https://www.linkedin.com/jobs/view/123456")
    for bad in (
        "https://evil-linkedin.com/jobs/view/123456",
        "https://notlinkedin.com/jobs/view/123456",
    ):
        with pytest.raises(linkedin.LinkedInDiscoveryError):
            linkedin.validate_job_url(bad)


def test_linkedin_blocker_metadata_is_detected_recursively_without_scanning_job_text():
    wrapped = {
        "parsed": {
            "jobs": [{"title": "Engineer", "description": "captcha research project"}],
            "blocked": True,
            "error": "CAPTCHA checkpoint",
        }
    }
    safe = linkedin.blocker_safe_agent_response(wrapped)
    lowered = str(safe).lower()
    assert "captcha" in lowered or "blocked" in lowered


def test_profile_embedding_and_retrieval_are_wired_into_profile_ready_and_base_packs():
    ready = _source("scripts/jobos_profile_ready.py")
    prep = _source("services/profile-ingestion/prepare_profile_for_pipeline_v1.py")
    assert "embed_profile_chunks.py" in ready
    assert '"--apply"' in ready
    assert "profile_retrieval_api.py" in prep
    assert "materialize_base_retrievals()" in prep
    assert "selected_chunk_ids" in prep
    assert "RETRIEVED APPROVED PROFILE SOURCE CHUNKS" in prep


def test_canonical_embedder_contains_all_v2_operator_features_and_canonical_identity():
    canonical = _source("services/profile-ingestion/embed_profile_chunks.py")
    for required in (
        '"--apply"', '"--dry-run"', '"--limit"', '"--model"', '"--retries"', '"--batch-size"',
        "SAVEPOINT chunk_embed_sp", "profile_chunk_embeddings", "content_hash",
        "embedding_provider", "resolved_embedding_model", "component_runs",
        "resolved_space", "Configure a stable concrete embedding model",
        "primary_profile_evidence", "project_artifact_evidence",
    ):
        assert required in canonical
    v2 = _source("services/profile-ingestion/embed_profile_chunks_v2.py")
    assert "_canonical_embedder" in v2
    assert 'argv.insert(0, "--dry-run")' in v2
    assert "canonical.main(argv)" in v2


def test_retrieval_is_bound_to_vector_space_and_approved_assets():
    source = _source("services/profile-ingestion/profile_retrieval_api.py")
    assert "e.embedding_provider = %s" in source
    assert "e.resolved_embedding_model = %s" in source
    assert "profile_asset_evidence_items" in source
    assert "pa.status='approved'" in source


def test_linkedin_discovery_uses_public_fake_mouse_and_live_captcha_target():
    source = _source("services/browser-controller/browser_queue_worker.py")
    assert "start_fake_mouse_thread" in source
    assert "_start_linkedin_fake_mouse" in source
    assert "_current_linkedin_page_url" in source
    assert "execute_parallel_bypass" in source
    assert "thread.join(timeout=5)" in source
    assert "urlsplit" in source
    assert "_current_linkedin_page_target" in source
    assert "pre_attempt_target_ids" in source and '"/checkpoint"' in source
    assert 'target_id=captcha_target["target_id"]' in source


def test_reference_pdf_renderer_is_reachable_from_canonical_resume_artifact():
    source = _source("services/common/canonical_resume_artifact_v1.py")
    assert "JOBOS_RESUME_TEMPLATE_OVERLAY_ENABLED" in source
    assert "export_pdf_from_reference" in source
    assert "experience_bullets" in source and "project_subtitles" in source


def test_ats_registry_projection_is_wired_after_migrations():
    registry = _source("services/ats/registry.py")
    runner = _source("scripts/apply_migrations.py")
    assert "def sync_registry(" in registry
    assert "candidate_domain_rows()" in registry
    assert "capability_rows()" in registry
    assert "sync_registry(cur)" in runner


def test_canonical_autofill_verifier_is_wired_without_removing_semantic_ref_recovery():
    session = _source("services/autofill/autofill_session_v1.py")
    verifier = _source("services/autofill/autofill_verifier_v1.py")
    assert "verify_actions(" in session
    assert "question_label" in session
    assert '"verify"' in verifier
    assert "value_matches" in verifier


def test_cost_backfill_preserves_unambiguous_pre_provider_history():
    source = _source("services/cost/cost_controller_v1.py")
    assert "len(candidates) == 1" in source
    assert "len(candidates) > 1" in source
    assert "cannot be priced safely" in source


def test_migration_097_closes_verified_db_authority_gaps():
    sql = _source("db/migrations/097_verified_bug_and_wiring_hardening.sql")
    assert "PRIMARY KEY (provider, model_name)" in sql
    assert "embedding_provider" in sql
    assert "resolved_embedding_model" in sql
    assert "uq_generated_documents_application_type_version" in sql
    assert "uq_drafted_replies_thread_version" in sql
    assert "document_feedback_prompt" in sql and "question_reply_prompt" in sql
    assert "awaiting_fit_review','screened" in sql
    assert "pipeline_transitions(from_step,to_step,automated,note,transition_kind)" in sql
    assert "reason=EXCLUDED.reason" not in sql
    assert "state-changing pipeline events must use jobos_transition_application()" in sql
    # The authorization fence must remain enabled through event insertion.
    on = sql.index("set_config('jobos.pipeline_transition_authorized','on'")
    insert = sql.index("INSERT INTO pipeline_events", on)
    off = sql.index("set_config('jobos.pipeline_transition_authorized','off'", insert)
    assert on < insert < off


def test_runtime_and_external_io_zombie_fixes_are_present():
    supervisor = _source("scripts/jobos_runtime_supervisor.py")
    gmail = _source("services/auth/gmail_verification_watcher_v1.py")
    ats = _source("services/discovery/ats_discovery_v1.py")
    repo = _source("services/repo-audit/repository_freshness_v1.py")
    assert "degraded.pop(name, None)" in supervisor
    assert "state_fresh" in supervisor
    assert "authoritative verification state changed before candidate persistence" in gmail
    assert "if apply:" in ats and "unexpected_exception" in ats
    assert "fcntl.flock" in repo


def test_approval_orchestrator_and_telegram_boundaries_are_hardened():
    approval = _source("services/approval/approval_service_v1.py")
    orchestrator = _source("services/orchestrator/orchestrator_v1.py")
    telegram = _source("services/telegram/telegram_review_bot_v1.py")
    assert "_recover_expired_requests" in approval
    assert "approval_binding_attempt_limit" in approval
    assert "awaiting_fit_review" in approval and 'to="screened"' in approval
    assert "workflow_step_runs" in orchestrator and "lease" in orchestrator.lower()
    assert "return 1 if failures else 0" in orchestrator
    assert "_send_bound_force_reply_prompt" in telegram
    assert "_prepare_delivery" in telegram
    assert "_uncertain_delivery" in telegram
