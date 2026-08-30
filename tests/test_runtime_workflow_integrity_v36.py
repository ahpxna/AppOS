from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_canonical_resume_helper_preserves_empty_edits_and_forwards_experience(monkeypatch, tmp_path):
    from services.common import canonical_resume_artifact_v1 as canonical

    calls = {}
    def fake_render_docx(**kwargs):
        calls.update(kwargs)
        kwargs["output"].write_bytes(b"docx")
    def fake_export_pdf(docx, output_dir):
        path = output_dir / "resume.pdf"
        path.write_bytes(b"pdf")
        return path

    monkeypatch.setattr(canonical.renderer, "render_docx", fake_render_docx)
    monkeypatch.setattr(canonical.renderer, "export_pdf", fake_export_pdf)
    template = tmp_path / "template.docx"
    template.write_bytes(b"template")
    exp = [{"slot": 1, "text": "Verified experience bullet"}]
    docx, pdf = canonical.render_canonical_resume(
        template=template,
        output_dir=tmp_path / "out",
        tailoring={"experience_bullets": exp, "project_bullets": []},
    )
    assert calls["experience_bullets"] == exp
    assert calls["project_bullets"] == []
    assert docx.read_bytes() == b"docx"
    assert pdf.read_bytes() == b"pdf"


def test_review_and_verified_resume_share_one_canonical_renderer():
    review = (ROOT / "services/review/render_review_artifacts_v1.py").read_text()
    export = (ROOT / "services/document-generation/render_verified_resume_v1.py").read_text()
    assert "render_canonical_resume" in review
    assert "render_canonical_resume" in export
    assert "no fixed-template project bullets" not in review
    assert "no fixed-template project bullets" not in export


def test_migration_076_removes_legacy_docs_bypass_and_adds_orchestrator_lease():
    sql = (ROOT / "db/migrations/076_workflow_integrity_orchestrator_leases_and_callback_binding.sql").read_text()
    assert "DELETE FROM pipeline_transitions" in sql
    assert "from_step='docs_verified' AND to_step='awaiting_approval'" in sql
    assert "requires_human=true" in sql
    assert "processing_run_id uuid" in sql
    assert "processing_lease_expires_at" in sql
    assert "llm_cost_reservations" in sql
    assert "source_sha256" in sql and "context_sha256" in sql


def test_orchestrator_completion_is_claim_and_state_cas_bound():
    source = (ROOT / "services/orchestrator/orchestrator_v1.py").read_text()
    state_store = (ROOT / "services/control_plane/pipeline_state.py").read_text()
    assert "def claim_application" in source
    assert "DEFAULT_PIPELINE_STATE_STORE.transition" in source
    assert "processing_run_id=%s::uuid" in state_store
    assert "processing_lease_expires_at>now()" in state_store
    assert "WHERE id=%s AND current_step=%s" in state_store
    assert "refusing stale completion" in source


def test_action_required_is_immutable_versioned_and_rematerializable():
    source = (ROOT / "services/review/review_service_v1.py").read_text()
    block = source[source.index("def ensure_action_required_review"):source.index("def sync_action_required")]
    assert "Superseded by a new exact handoff source" in block
    assert "status='rejected'" in block
    assert "status IN ('approved','resolved','expired')" not in block
    assert "ON CONFLICT" not in block


def test_telegram_callback_binds_review_source_and_delivery_context():
    source = (ROOT / "services/telegram/telegram_review_bot_v1.py").read_text()
    assert "source_sha256, context_sha256" in source
    assert "Review content changed; use the newest message" in source
    assert "Approval context changed; use the newest message" in source


def test_shared_ats_jobs_label_stays_generic_verification():
    from services.auth.gmail_verification_v1 import _relevance_tier
    message = {
        "from": "no-reply@mailer.example",
        "subject": "Jobs verification code",
        "body": "Verify your email for your candidate account. Verification code 481293",
    }
    assert _relevance_tier(message, employer_domain="https://jobs.ashbyhq.com") == "generic_verification"


def test_gmail_denial_has_durable_candidate_rejection_hook():
    source = (ROOT / "services/approval/approval_service_v1.py").read_text()
    assert "def _reject_email_candidate_for_denied_request" in source
    assert source.count("_reject_email_candidate_for_denied_request(") >= 3
    assert "status='rejected'" in source


def test_cost_backfill_stops_at_direct_accounting_migration():
    source = (ROOT / "services/cost/cost_controller_v1.py").read_text()
    assert "076_workflow_integrity_orchestrator_leases_and_callback_binding.sql" in source
    assert "cr.created_at < COALESCE" in source
    gateway = (ROOT / "services/common/llm_gateway.py").read_text()
    assert "reserve_paid_call" in gateway
    assert "settle_paid_call" in gateway
    assert "LLMResult" in gateway


def test_doctor_uses_dynamic_latest_migration_and_configured_template():
    source = (ROOT / "scripts/jobos.py").read_text()
    assert "def _latest_migration_contract" in source
    assert 'JOBOS_RESUME_TEMPLATE_PATH' in source
    assert 'choices=("core","documents","browser","production")' in source


def test_verify_pipeline_defaults_to_core_profile():
    source = (ROOT / "scripts/verify_pipeline.sh").read_text()
    assert 'JOBOS_VERIFY_PROFILE:-core' in source
    assert 'JOBOS_PYTHON:-.venv/bin/python' in source
