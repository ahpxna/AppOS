from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

os.environ.setdefault("JOBOS_DB_PASSWORD", "test")


@dataclass
class Action:
    action: str
    ref: str
    value: str | None
    profile_key: str | None
    reason: str = ""
    question_label: str | None = None


def test_autofill_scope_is_exact_and_upload_is_never_inherited():
    from services.common.autofill_action_scope import build_exact_action_scope, action_is_exactly_approved

    reviewed = Action("fill", "email-ref-v1", "me@example.com", "personal.email", question_label="Email")
    scope = build_exact_action_scope([reviewed])
    assert action_is_exactly_approved(reviewed, scope)
    assert not action_is_exactly_approved(
        Action("fill", "email-ref-v2", "me@example.com", "personal.email", question_label="Email"), scope
    )
    assert not action_is_exactly_approved(
        Action("fill", "email-ref-v1", "other@example.com", "personal.email", question_label="Email"), scope
    )
    assert not action_is_exactly_approved(
        Action("upload", "resume-ref-v1", "/tmp/resume.pdf", "documents.resume", question_label="Resume"), scope
    )


def test_submit_confirmation_rejects_static_instructional_text():
    from services.application_actions.privileged_action_v1 import _confirmation

    before = {"snapshot": '- button "Submit application" [ref=s1]\n- generic "Application submitted status will appear in your dashboard"'}
    after_same_instruction = {"snapshot": '- button "Submit application" [ref=s1]\n- generic "Application submitted status will appear in your dashboard"'}
    assert not _confirmation(
        before_snapshot=before, before_url="https://jobs.example.com/apply",
        after_snapshot=after_same_instruction, after_url="https://jobs.example.com/apply", submit_ref="s1",
    )

    after_confirmation = {"snapshot": '- heading "Thank you for applying"\n- generic "We received your application"'}
    assert _confirmation(
        before_snapshot=before, before_url="https://jobs.example.com/apply",
        after_snapshot=after_confirmation, after_url="https://jobs.example.com/apply/thank-you", submit_ref="s1",
    )


def test_telegram_no_screenshot_still_allows_autofill_approval_and_new_gates():
    from services.telegram import telegram_review_bot_v1 as tg

    class Cur:
        def __init__(self): self.last_sql = ""
        def execute(self, sql, *args, **kwargs): self.last_sql = str(sql)
        def fetchone(self):
            if "source_sha256" in self.last_sql:
                return ("source-sha",)
            return None

    cur = Cur()
    autofill = tg._keyboard(cur, "r1", 7, "autofill_review", {"execution_state": "completed"})
    assert "Approve" in autofill
    app_ready = tg._keyboard(cur, "r2", 7, "application_ready", {})
    assert "PREPARE NEXT GATE" in app_ready
    recon = tg._keyboard(cur, "r3", 7, "reconciliation_required", {"privileged_execution_id": "e1"})
    assert "OCCURRED" in recon and "NOT OCCURRED" in recon
    upload = tg._keyboard(cur, "r4", 7, "approval_request", {"approval_type": "privileged_upload_document"})
    assert "UPLOAD DOCUMENT" in upload


def test_gmail_refetch_uses_exact_approved_mailbox(monkeypatch):
    from services.auth import gmail_verification_v1 as gmail

    code = "481293"
    observed = []

    def fake_read(message_id, sanitized, account=None):
        observed.append(account)
        return {"id": message_id, "body": f"Your verification code is {code}"}

    monkeypatch.setattr(gmail, "read_message", fake_read)
    candidate = {
        "gmail_account": "approved@example.com",
        "gmail_message_id": "m1",
        "verification_kind": "numeric_code",
        "secret_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "secret_context": {},
    }
    assert gmail.refetch_secret(candidate) == code
    assert observed == ["approved@example.com"]


def test_privileged_upload_and_state_machine_are_wired_in_source():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    action_request = (root / "services/application_actions/action_request_v1.py").read_text()
    executor = (root / "services/application_actions/privileged_action_v1.py").read_text()
    review = (root / "services/review/review_service_v1.py").read_text()
    telegram = (root / "services/telegram/telegram_review_bot_v1.py").read_text()

    assert '"privileged_upload_document"' in action_request
    assert 'elif atype == "privileged_upload_document"' in executor
    assert "pipeline_transitions" in executor and "pipeline_events" in executor
    assert '_require_application_step(cur, application_id, "application_ready")' in executor
    assert "_document_bindings_still_current" in executor
    assert "privileged_execution_id" in review and "allowed_outcomes" in review
    assert "single privileged-action worker owns browser I/O" in telegram
    assert "execute_one(conn, approval_request_id)" not in telegram
    assert "expires_at > now()" in telegram
