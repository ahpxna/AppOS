from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_batch_safe_policy_never_hides_irreversible_or_security_actions():
    from services.review.ux_policy_v1 import is_batch_safe_item
    assert is_batch_safe_item(item_type="document_review", payload={"qa_status": "pass"})
    assert is_batch_safe_item(item_type="approval_request", payload={"approval_type": "autofill_form"})
    assert is_batch_safe_item(item_type="approval_request", payload={
        "approval_type": "privileged_upload_document", "delegated_to_autofill": True,
    })
    assert not is_batch_safe_item(item_type="approval_request", payload={
        "approval_type": "privileged_upload_document", "delegated_to_autofill": False,
    })
    for action in (
        "privileged_submit_application", "privileged_accept_terms",
        "privileged_login_employer_account", "privileged_trust_external_domain",
        "privileged_use_email_verification",
    ):
        assert not is_batch_safe_item(item_type="approval_request", payload={"approval_type": action})


def test_resume_diff_is_structured_and_baseline_is_explicit():
    from services.review.ux_policy_v1 import resume_change_lines
    assert "baseline resume" in resume_change_lines({})[0]
    lines = resume_change_lines({"resume_template": {"experience_bullets": [{
        "slot": 1, "previous_bullet": "Old bullet", "text": "New bullet",
    }]}})
    assert any("− Old bullet" in line for line in lines)
    assert any("+ New bullet" in line for line in lines)


def test_quick_question_choices_reduce_typing_without_auto_answering():
    from services.review.ux_policy_v1 import quick_question_choices
    assert quick_question_choices("Are you willing to travel?") == ["Yes", "No"]
    assert quick_question_choices("Expected salary?", salary_target=135000) == ["$135,000"]
    assert quick_question_choices("Tell us why this company interests you") == []


def test_daily_control_surface_migration_is_progressive_and_version_bound():
    migration = (ROOT / "db/migrations/077_daily_control_surface_and_review_ux.sql").read_text()
    assert "snoozed_until" in migration
    assert "telegram_ui_tokens" in migration
    assert "telegram_control_surface_state" in migration
    assert "payload_json" in migration
    assert "details" in migration and "answer" in migration and "other" in migration
    assert "hri.snoozed_until IS NULL" in migration


def test_telegram_daily_ux_has_dashboard_batch_details_and_natural_reply():
    source = (ROOT / "services/telegram/telegram_review_bot_v1.py").read_text()
    assert "def dispatch_dashboard" in source
    assert "Approve {len(safe)} safe" in source
    assert "Review next" in source
    assert "def _send_review_details" in source
    assert "Reply directly to this message with your answer" in source
    assert '"force_reply": True' in source
    assert "One-tap safe batch approval" in source
    assert "Daily-use card: short, decision-first, no internal IDs." in source


def test_auth_state_watcher_is_read_only_and_exact_target_bound():
    source = (ROOT / "services/auth/browser_state_watcher_v1.py").read_text()
    assert "target_id" in source
    assert "purpose=\"employer_handoff\"" in source
    assert "_snapshot" in source
    assert ".click(" not in source
    assert "Input.dispatch" not in source


def test_jobos_start_stop_supervisor_exists_and_restarts_configured_workers():
    jobos = (ROOT / "scripts/jobos.py").read_text()
    supervisor = (ROOT / "scripts/jobos_runtime_supervisor.py").read_text()
    worker = (ROOT / "services/orchestrator/orchestrator_worker_v1.py").read_text()
    assert 'commands.add_parser("start"' in jobos
    assert 'commands.add_parser("stop"' in jobos
    assert "services.orchestrator.orchestrator_worker_v1" in supervisor
    assert "browser_queue_worker.py" in supervisor
    assert "privileged_action_v1" in supervisor
    assert "filter\", \"--all\", \"--apply" in worker
    assert "advance\", \"--all\", \"--apply" in worker
