from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generic_verification_mail_is_not_employer_bound():
    from services.auth.gmail_verification_v1 import _relevance_tier

    generic = {
        "headers": {"from": "security@unrelated.example", "subject": "Verify your email"},
        "body": "Verification code 123456 for your candidate account",
    }
    employer = {
        "headers": {"from": "jobs@acme.example", "subject": "Acme verification code"},
        "body": "Verify your email for Acme candidate account",
    }
    assert _relevance_tier(generic, employer_domain="https://acme.example") == "generic_verification"
    assert _relevance_tier(employer, employer_domain="https://acme.example") == "employer_match"


def test_final_submit_binds_only_primary_resume(monkeypatch):
    import services.application_actions.privileged_action_v1 as action

    resume = {"artifact_id": "resume-artifact", "source_jd_hash": "jd", "application_jd_hash": "jd"}
    cover = {"artifact_id": "cover-artifact", "source_jd_hash": "old", "application_jd_hash": "jd"}
    monkeypatch.setattr(action, "_document_bindings", lambda *_a, **_k: {"resume": resume, "cover_letter": cover})
    seen = {}

    def current(_cur, _app, approved, *, required_types=None):
        seen["approved"] = approved
        seen["required_types"] = required_types
        return required_types == {"resume"}

    monkeypatch.setattr(action, "_document_bindings_still_current", current)
    assert action._submission_document_bindings(object(), "app") == {"resume": resume}
    assert seen["required_types"] == {"resume"}
    assert "cover_letter" not in seen["approved"]


def test_consent_executor_verifies_toggles_before_affirmative_navigation():
    source = (ROOT / "services/application_actions/privileged_action_v1.py").read_text(encoding="utf-8")
    block = source[source.index('elif atype == "privileged_accept_terms"'):
                   source.index('elif atype == "privileged_advance_application_step"')]
    assert "live_toggle_snapshot = transport.snapshot(target_id)" in block
    assert "approved consent toggle" in block
    assert block.index("live_toggle_snapshot = transport.snapshot(target_id)") < block.index("if button_items:")
    assert "multiple affirmative consent buttons are ambiguous" in block


def test_action_required_handoff_is_migrated_and_non_executable():
    migration = (ROOT / "db/migrations/075_action_required_review_handoff.sql").read_text(encoding="utf-8")
    review = (ROOT / "services/review/review_service_v1.py").read_text(encoding="utf-8")
    watcher = (ROOT / "services/auth/gmail_verification_watcher_v1.py").read_text(encoding="utf-8")
    assert "'action_required'" in migration
    assert "open_apply_binding_required" in review
    assert "email_verification_binding_required" in review
    assert "Fresh exact-bound privileged approval prepared separately; this handoff performed no browser I/O." in review
    assert "OTP found — refocus the verification page" in watcher


def test_doctor_requires_latest_migration_dynamically():
    source = (ROOT / "scripts/jobos.py").read_text(encoding="utf-8")
    assert "def _latest_migration_contract" in source
    assert "migration_check_name = f\"Migrations through {latest_number}\"" in source
