from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.common.application_browser_binding_v1 import (
    ApplicationBrowserBindingError,
    resolve_application_bound_target,
)
from services.review import document_revision_worker_v1 as revision_worker
from services.review.document_revision_worker_v1 import _generated_document_id


class FakeCursor:
    def __init__(self, *, auth_row=None, task_rows=None, exact_task_rows=None):
        self.auth_row = auth_row
        self.task_rows = list(task_rows or [])
        self.exact_task_rows = list(exact_task_rows or [])
        self._one = None
        self._all = []

    def execute(self, query, params=()):
        q = " ".join(str(query).split())
        if "FROM application_auth_sessions" in q:
            self._one = self.auth_row
            self._all = []
        elif "FROM browser_tasks" in q and "WHERE id=%s" in q:
            self._one = None
            self._all = list(self.exact_task_rows)
        elif "FROM browser_tasks" in q:
            self._one = None
            self._all = list(self.task_rows)
        else:
            raise AssertionError(f"unexpected query: {q}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


@dataclass
class FakeTarget:
    target_id: str
    url: str


class FakeTransport:
    def __init__(self, urls: dict[str, str], focused: str | None = None):
        self.urls = dict(urls)
        self.focused = focused

    def current_url(self, target_id: str) -> str:
        if target_id not in self.urls:
            raise RuntimeError("target missing")
        return self.urls[target_id]

    def resolve_target(self):
        if not self.focused:
            raise RuntimeError("no focus")
        return FakeTarget(self.focused, self.urls[self.focused])


def test_binding_checks_each_durable_url_before_deduping_same_target():
    cur = FakeCursor(
        auth_row=("https://ats.example/old", {"target_id": "tab-1"}),
        task_rows=[("tab-1", "https://ats.example/current", None)],
    )
    transport = FakeTransport({"tab-1": "https://ats.example/current"})

    bound = resolve_application_bound_target(cur, transport, application_id="app-1")

    assert bound.target_id == "tab-1"
    assert bound.current_url == "https://ats.example/current"
    assert bound.source == "autofill_task"


def test_binding_refuses_two_distinct_live_tabs_even_with_same_url():
    cur = FakeCursor(
        auth_row=("https://ats.example/app", {"target_id": "tab-a"}),
        task_rows=[("tab-b", "https://ats.example/app", None)],
    )
    transport = FakeTransport({
        "tab-a": "https://ats.example/app",
        "tab-b": "https://ats.example/app",
    })

    with pytest.raises(ApplicationBrowserBindingError, match="multiple live browser targets"):
        resolve_application_bound_target(cur, transport, application_id="app-1")


def test_exact_browser_task_binding_wins_for_manual_question_recheck():
    cur = FakeCursor(
        auth_row=("https://ats.example/stale", {"target_id": "tab-old"}),
        exact_task_rows=[("tab-question", "https://ats.example/form", None)],
    )
    transport = FakeTransport({
        "tab-old": "https://ats.example/other",
        "tab-question": "https://ats.example/form",
    })

    bound = resolve_application_bound_target(
        cur, transport, application_id="app-1", browser_task_id="task-1"
    )

    assert bound.target_id == "tab-question"
    assert bound.source == "exact_autofill_task"


def test_human_refocus_requires_exact_expected_url():
    cur = FakeCursor(auth_row=None, task_rows=[])
    transport = FakeTransport({"focused": "https://ats.example/wrong"}, focused="focused")

    with pytest.raises(ApplicationBrowserBindingError, match="does not match the exact application URL"):
        resolve_application_bound_target(
            cur,
            transport,
            application_id="app-1",
            allow_focused_rebind=True,
            expected_url="https://ats.example/expected",
        )


def test_document_revision_worker_parses_exact_generator_document_id():
    output = "noise\ngenerated_document_id: 123e4567-e89b-12d3-a456-426614174000\nversion: 9\n"
    assert _generated_document_id(output) == "123e4567-e89b-12d3-a456-426614174000"
    assert _generated_document_id("generator completed without id") is None


def test_document_revision_subprocess_timeout_is_transient_not_worker_crash(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1800)

    monkeypatch.setattr(revision_worker.subprocess, "run", boom)
    ok, detail, transient = revision_worker._run(["fake"])
    assert ok is False
    assert transient is True
    assert "timed out" in detail


def test_optional_consent_is_not_packaged_as_required_legal_gate():
    from services.application_actions.privileged_action_v1 import _consent_items

    nodes = [
        {"role": "checkbox", "ref": "opt", "label": "I consent to future opportunities", "required": False, "selected": False},
        {"role": "checkbox", "ref": "req", "label": "I agree to the Terms", "required": True, "selected": False},
    ]
    items = _consent_items(nodes)
    assert [item["ref"] for item in items] == ["req"]


def test_migration_079_removes_legacy_autofill_and_submit_bypasses():
    from pathlib import Path

    sql = Path("db/migrations/079_pipeline_identity_runtime_questions_and_document_feedback.sql").read_text()
    assert "from_step='awaiting_approval' AND to_step='form_filled'" in sql
    assert "from_step='form_filled' AND to_step='submitted'" in sql


def test_document_revision_feedback_uses_stdin_not_process_argv():
    from pathlib import Path

    worker = Path("services/review/document_revision_worker_v1.py").read_text()
    generator = Path("services/document-generation/generate_documents_v1.py").read_text()
    assert '"--revision-feedback-stdin"' in worker
    assert 'input_text=str(feedback)' in worker
    assert '--revision-feedback-stdin' in generator
