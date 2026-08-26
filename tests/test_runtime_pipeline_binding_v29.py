from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def test_supplemental_document_missing_soft_degrades(tmp_path):
    from services.autofill.autofill_context_v1 import load_autofill_context

    class Cur:
        def __init__(self):
            self.sql = ""
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
        def fetchall(self):
            if "v_autofill_ready_values" in self.sql:
                return []
            if "generated_document_artifacts gda" in self.sql and "approved_resume_artifact_id" in self.sql:
                return [("cover_letter", str(tmp_path / "missing.pdf"), "missing.pdf", "a" * 64)]
            if "application_question_memory" in self.sql:
                return []
            if "sensitive_answers" in self.sql:
                return []
            return []
        def fetchone(self):
            if "FROM immigration_profiles" in self.sql:
                return None
            if "lower(coalesce(company" in self.sql:
                return ("example", "custom")
            if "SELECT jd_hash FROM applications" in self.sql:
                return ("j" * 64,)
            return None

    ctx = load_autofill_context(
        Cur(), application_id="app", artifact_binding=None,
        document_sha256="d" * 64, page_url="https://ats.example/apply",
        page_fingerprint_sha256="f" * 64, data_root=tmp_path,
    )
    assert ctx.profile["documents"] == {}


def test_autofill_scope_contains_upload_identity_but_requires_child_at_worker():
    from services.autofill.autofill_planner_v1 import PlannedAction
    from services.common.autofill_action_scope import build_exact_action_scope, action_is_exactly_approved

    upload = PlannedAction("upload", "resume-ref", "/tmp/resume.pdf", "documents.resume", "", "Resume")
    scope = build_exact_action_scope([upload])
    assert scope["version"] == 3
    assert scope["document_types"] == ["resume"]
    assert action_is_exactly_approved(upload, scope) is True
    # Parent scope is only an exact identity. Browser worker separately checks
    # a live delegated privileged_upload_document child before permitting I/O.


def test_autofill_approval_rejects_stale_pipeline_step_before_other_binding_queries():
    from services.approval.approval_service_v1 import assert_binding_matches

    class Cur:
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
        def fetchone(self):
            if "current_step FROM applications" in self.sql:
                return ("https://jobs.example/1", "j" * 64, "application_ready")
            raise AssertionError(f"unexpected query after stale step: {self.sql}")

    with pytest.raises(RuntimeError, match="pipeline step changed"):
        assert_binding_matches(Cur(), {
            "type": "autofill_form", "application_id": "app",
            "payload_json": {
                "application_job_url": "https://jobs.example/1",
                "application_jd_hash": "j" * 64,
                "expected_application_step": "awaiting_approval",
            },
        })


def test_delegated_upload_cannot_execute_as_standalone_privileged_action(monkeypatch):
    from services.application_actions import privileged_action_v1 as privileged

    monkeypatch.setattr(privileged, "_transport", lambda: object())

    class Cur:
        def __init__(self):
            self.calls = 0
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=None):
            self.calls += 1
            assert self.calls == 1, "delegated child must refuse before creating an execution row"
        def fetchone(self):
            return (
                "req", "privileged_upload_document", "app", "approved",
                datetime.now(timezone.utc) + timedelta(minutes=5),
                {"delegated_to_autofill": True},
            )

    class Conn:
        def cursor(self): return Cur()

    with pytest.raises(privileged.PrivilegedActionError, match="parent autofill session"):
        privileged.execute_one(Conn(), "req")


def test_auth_page_with_login_and_create_materializes_both_human_choices(monkeypatch):
    from services.application_actions import privileged_action_v1 as privileged

    created = []
    monkeypatch.setattr(privileged, "_capture_review_screenshot", lambda *a, **k: None)
    monkeypatch.setattr(
        privileged, "_account_action_payload",
        lambda _cur, _nodes, _url, *, action: ({"control_ref": action + "-ref", "consent_blockers": []}, []),
    )
    monkeypatch.setattr(
        privileged, "create_privileged_request",
        lambda _cur, **kwargs: created.append(kwargs) or f"r{len(created)}",
    )
    nodes = [
        {"label": "Sign in"},
        {"label": "Create account"},
    ]
    privileged._enqueue_state_followup(
        object(), object(), application_id="app", target_id="tab",
        url="https://ats.example/login", fingerprint="f" * 64,
        nodes=nodes, state="needs_account_auth",
    )
    assert [item["action_type"] for item in created] == [
        "privileged_login_employer_account", "privileged_create_employer_account"
    ]


def test_parent_autofill_waits_while_expected_upload_child_is_pending(monkeypatch):
    from services.approval import approval_service_v1 as approval

    expected = [{"field_ref": "resume", "document_type": "resume", "artifact_id": "a1", "sha256": "a" * 64}]
    child = {**expected[0], "autofill_plan_key": "plan", "delegated_to_autofill": True, "parent_approval_request_id": "parent"}

    class Cur:
        def __init__(self): self.query = ""; self.rowcount = 0
        def execute(self, sql, params=None): self.query = " ".join(sql.split()); self.rowcount = 0
        def fetchall(self):
            if "type='privileged_upload_document'" in self.query:
                return [("child", child, "pending", None)]
            return []
        def fetchone(self):
            if "type='autofill_form'" in self.query:
                return ("parent", {"expected_upload_capabilities": expected}, "approved")
            return None

    monkeypatch.setattr(approval, "_repair_delegated_children_for_parent", lambda *_a, **_k: [])
    monkeypatch.setattr(approval, "assert_binding_matches", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not bind/queue while child pending")))
    assert approval.queue_ready_autofill_for_plan(Cur(), application_id="app", plan_key="plan", actor="test") is False


def test_parent_autofill_queues_after_all_expected_upload_children_resolved(monkeypatch):
    from services.approval import approval_service_v1 as approval

    expected = [{"field_ref": "resume", "document_type": "resume", "artifact_id": "a1", "sha256": "a" * 64}]
    child = {**expected[0], "autofill_plan_key": "plan", "delegated_to_autofill": True, "parent_approval_request_id": "parent"}
    parent_payload = {"expected_upload_capabilities": expected}

    class Cur:
        def __init__(self): self.query = ""; self.rowcount = 0
        def execute(self, sql, params=None): self.query = " ".join(sql.split()); self.rowcount = 0
        def fetchall(self):
            if "type='privileged_upload_document'" in self.query:
                return [("child", child, "approved", None)]
            return []
        def fetchone(self):
            if "type='autofill_form'" in self.query:
                return ("parent", parent_payload, "approved")
            return None

    monkeypatch.setattr(approval, "_repair_delegated_children_for_parent", lambda *_a, **_k: [])
    monkeypatch.setattr(approval, "assert_binding_matches", lambda *_a, **_k: None)
    queued = []
    monkeypatch.setattr(approval, "_queue_autofill_task", lambda _cur, **kwargs: queued.append(kwargs) or True)
    assert approval.queue_ready_autofill_for_plan(Cur(), application_id="app", plan_key="plan", actor="test") is True
    assert queued[0]["request_id"] == "parent"


def test_privileged_worker_delegated_filter_is_null_safe():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "services/application_actions/privileged_action_v1.py").read_text()
    assert "COALESCE(ar.payload_json->>'delegated_to_autofill','false')='true'" in source


def test_autofill_session_marks_child_before_upload_io():
    from services.autofill.autofill_executor_v1 import BrowserTarget
    from services.autofill.autofill_planner_v1 import PlannedAction
    from services.autofill.autofill_session_v1 import AutofillSession, SnapshotState
    from services.autofill.form_inspector_v1 import FormField

    events = []

    class Transport:
        def __init__(self): self.value = ""
        def resolve_target(self): return BrowserTarget("tab", "https://jobs.example/apply")
        def focus(self, target_id):
            assert target_id == "tab"
            return BrowserTarget("tab", "https://jobs.example/apply")
        def current_url(self, _target): return "https://jobs.example/apply"
        def execute(self, _target, command):
            events.append("io")
            self.value = command["value"]

    transport = Transport()
    def state(_target):
        return SnapshotState((FormField("upload-ref", "Resume", "file", transport.value),), (), "f" * 64)

    action = PlannedAction("upload", "upload-ref", "/tmp/resume.pdf", "documents.resume", "", "Resume")
    session = AutofillSession(
        transport=transport, expected_target_id="tab", expected_origin="https://jobs.example",
        expected_initial_url="https://jobs.example/apply", expected_page_fingerprint="f" * 64,
        snapshot_state=state, origin_allowed=lambda _url: None,
        begin_execution=lambda _target: events.append("parent-executing"),
        before_action=lambda *_args: events.append("journal") or "j1",
        before_io=lambda *_args: events.append("child-executing"),
        after_verified=lambda *_args: events.append("verified"),
        after_failed=lambda *_args: events.append("failed"),
    )
    result = session.execute(lambda _state: [action])
    assert result.status == "completed"
    assert events == ["parent-executing", "journal", "child-executing", "io", "verified"]
