from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_transition_helper_calls_use_keyword_only_application_and_step():
    tree = ast.parse((ROOT / "services/review/review_service_v1.py").read_text())
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name == "_transition_application_step":
                calls.append(node)
    assert calls
    for call in calls:
        assert len(call.args) == 1  # only cur is positional
        names = {kw.arg for kw in call.keywords}
        assert {"application_id", "to_step"} <= names


def test_072_adds_later_page_auth_edges_and_scoped_magic_link_trust():
    sql = (ROOT / "db/migrations/072_pipeline_recovery_and_scoped_email_trust.sql").read_text()
    for step in ("needs_account_auth", "needs_email_verification", "needs_mfa", "needs_human_checkpoint"):
        assert f"('application_ready', '{step}'" in sql
    assert "application_scoped_domain_trusts" in sql
    assert "approval_request_id" in sql
    assert "expires_at" in sql


def test_doctor_uses_real_unresolved_check_and_has_strict_exit_modes():
    source = (ROOT / "scripts/jobos.py").read_text()
    assert '"No unresolved browser action"' in source
    assert '"No unresolved autofill task"' not in source
    assert '--strict' in source
    assert '--require-autofill' in source
    for label in (
        "CORE READY", "DOCUMENT READY", "FORM-FILL READY", "AUTH FLOW READY",
        "PRIVILEGED ACTION READY", "EMAIL VERIFICATION READY", "SUBMIT READY",
    ):
        assert label in source


def test_persist_candidate_uses_psycopg_jsonb(monkeypatch):
    from services.auth import gmail_verification_v1 as gmail

    captured = {}

    class Cur:
        def execute(self, _sql, params):
            captured["params"] = params

        def fetchone(self):
            return ("candidate-id",)

    monkeypatch.setattr(gmail, "gmail_account", lambda: "candidate@example.com")
    candidate = {
        "message_id": "m1", "sender": "sender@example.com", "subject": "Verify",
        "received_at": datetime.now(timezone.utc), "kind": "numeric_code",
        "secret_sha256": "a" * 64, "secret_context": {"kind": "numeric_code", "digits": 6},
    }
    assert gmail.persist_candidate(Cur(), application_id="app", candidate=candidate) == "candidate-id"
    bound = captured["params"][-1]
    assert type(bound).__name__ == "Jsonb"


def test_gmail_candidate_without_parseable_timestamp_soft_degrades(monkeypatch):
    from services.auth import gmail_verification_v1 as gmail

    monkeypatch.setattr(gmail, "search_candidate_ids", lambda **_kwargs: ["old-or-unknown"])
    monkeypatch.setattr(gmail, "read_message", lambda *_args, **_kwargs: {
        "id": "old-or-unknown", "from": "recruiting@example.com",
        "subject": "Verification code", "body": "Your code is 123456",
    })
    result = gmail.discover_verification(
        recipient="candidate@example.com", requested_at=datetime.now(timezone.utc),
        employer_origin="https://example.com", max_results=3,
    )
    assert result is None


def test_capsolver_reads_env_at_call_time_and_binds_http_timeout(monkeypatch):
    from services.autofill import capsolver_api

    calls = []

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    monkeypatch.setattr(capsolver_api, "load_repo_env", lambda: monkeypatch.setenv("CAPSOLVER_API_KEY", "late-key"))
    monkeypatch.setenv("CAPSOLVER_REQUEST_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("CAPSOLVER_SOLVE_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CAPSOLVER_POLL_INTERVAL_SECONDS", "1")
    responses = iter([Resp({"taskId": "t1"}), Resp({"status": "ready", "solution": {"token": "ok"}})])

    def post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return next(responses)

    monkeypatch.setattr(capsolver_api.requests, "post", post)
    monkeypatch.setattr(capsolver_api.time, "sleep", lambda _n: None)
    assert capsolver_api.solve_captcha("https://www.linkedin.com/jobs", "site", "FunCaptchaTaskProxyless") == "ok"
    assert all(call[2] == 7 for call in calls)
    assert all(call[1]["clientKey"] == "late-key" for call in calls)


def test_parallel_bypass_exact_page_selection_and_js_escaping(monkeypatch):
    from services.autofill import parallel_bypass

    tabs = [
        {"type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://wrong"},
        {"type": "page", "url": "https://www.linkedin.com/jobs/search/", "webSocketDebuggerUrl": "ws://right"},
    ]
    assert parallel_bypass._select_exact_page(tabs, "https://www.linkedin.com/jobs/search/?keywords=x") == "ws://right"
    with pytest.raises(RuntimeError):
        parallel_bypass._select_exact_page(tabs, "https://www.linkedin.com/jobs/view/123")

    sent = []

    class WS:
        def settimeout(self, _timeout):
            pass

        def send(self, payload):
            sent.append(json.loads(payload))

        def recv(self):
            return json.dumps({"result": {"result": {"value": {"ok": True}}}})

        def close(self):
            pass

    monkeypatch.setattr(parallel_bypass.websocket, "create_connection", lambda *_a, **_k: WS())
    token = "a'b\\c"
    parallel_bypass._inject_solution("ws://right", token, "FunCaptchaTaskProxyless")
    expression = sent[0]["params"]["expression"]
    assert json.dumps(token) in expression
    assert "fc-token" in expression
    assert "g-recaptcha-response" not in expression


def test_application_ready_with_review_and_submit_materializes_two_separate_capabilities(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    monkeypatch.setattr(action, "_require_application_step", lambda *_a, **_k: None)
    monkeypatch.setattr(action, "_transport", lambda: object())
    monkeypatch.setattr(action, "_base_binding", lambda _t: (
        "target", "https://ats.example/app", {}, [], "fp",
    ))
    monkeypatch.setattr(action, "_require_trusted_target", lambda *_a, **_k: None)
    monkeypatch.setattr(action, "_consent_items", lambda _nodes: [])
    monkeypatch.setattr(action, "_clickables", lambda _nodes: [
        {"ref": "review", "label": "Review application"},
        {"ref": "submit", "label": "Submit application"},
    ])
    created = []

    def prepare_exact(_cur, *, application_id, action: str, control):
        created.append((action, control["ref"]))
        return f"approval-{action}-{control['ref']}"

    monkeypatch.setattr(action, "_prepare_exact_application_ready_control", prepare_exact)
    ids = action.materialize_application_ready_gate(object(), "app")
    assert ids == ["approval-advance_application_step-review", "approval-submit_application-submit"]
    assert created == [("advance_application_step", "review"), ("submit_application", "submit")]


def test_reconciliation_begin_application_reconstructs_from_fresh_browser_state(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    class Cur:
        def execute(self, _sql, _params=None):
            pass

        def fetchone(self):
            return ({"target_id": "old", "expected_url": "https://jobs.example/role"},)

    class Focused:
        target_id = "new"

    class Transport:
        def resolve_target(self):
            return Focused()

    monkeypatch.setattr(action, "_transport", lambda: Transport())
    monkeypatch.setattr(action, "_snapshot", lambda _t, _id: (
        "https://ats.example/app", {"snapshot": "form"}, [], "fp2",
    ))
    monkeypatch.setattr(action, "_application_step", lambda *_a, **_k: "docs_verified")
    transitions = []
    monkeypatch.setattr(action, "_transition_application_step", lambda _cur, **kw: transitions.append(kw) or True)
    monkeypatch.setattr(action, "detect_platform", lambda *_a: "custom")
    monkeypatch.setattr(action, "detect_page_state", lambda *_a: ("application_form_ready", {"input_count": 3}))
    updates = []
    monkeypatch.setattr(action, "_update_auth_session", lambda _cur, **kw: updates.append(kw))
    monkeypatch.setattr(action, "_host_is_allowed", lambda *_a, **_k: True)

    result = action.reconcile_observed_privileged_effect(
        Cur(), application_id="app", approval_request_id="approval", action_type="privileged_begin_application"
    )
    assert transitions[0]["to_step"] == "application_entrypoint_ready"
    assert updates[0]["state"] == "application_form_ready"
    assert result["state"] == "application_form_ready"
    assert result["followup"] == "state_gate"


def test_capsolver_processing_has_hard_deadline(monkeypatch):
    from services.autofill import capsolver_api

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    monkeypatch.setattr(capsolver_api, "load_repo_env", lambda: monkeypatch.setenv("CAPSOLVER_API_KEY", "deadline-key"))
    monkeypatch.setenv("CAPSOLVER_SOLVE_TIMEOUT_SECONDS", "15")
    monkeypatch.setattr(capsolver_api.requests, "post", lambda url, json=None, timeout=None: (
        Resp({"taskId": "t1"}) if url.endswith("createTask") else Resp({"status": "processing"})
    ))
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(capsolver_api.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        capsolver_api.solve_captcha("https://www.linkedin.com/jobs", "site", "FunCaptchaTaskProxyless")


def test_gmail_watcher_releases_read_transaction_before_network(monkeypatch):
    from services.auth import gmail_verification_watcher_v1 as watcher

    now = datetime.now(timezone.utc)

    class Cur:
        def execute(self, _sql, _params=None):
            pass

        def fetchall(self):
            return [("app", "candidate@example.com", "https://example.com", "https://example.com/verify", "fp", {"target_id": "t"}, now)]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Conn:
        def __init__(self):
            self.commits = 0

        def cursor(self):
            return Cur()

        def commit(self):
            self.commits += 1

    conn = Conn()

    def discover(**_kwargs):
        assert conn.commits >= 1
        return None

    monkeypatch.setattr(watcher, "discover_verification", discover)
    assert watcher.process_pending(conn, max_results=3) == 0
    assert conn.commits == 1


def test_frozen_fake_mouse_helper_refuses_ambiguous_linkedin_tabs(monkeypatch):
    from services.autofill import parallel_bypass

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"type": "page", "url": "https://www.linkedin.com/jobs/search/", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/a"},
                {"type": "page", "url": "https://www.linkedin.com/jobs/view/1", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/b"},
            ]

    monkeypatch.setattr(parallel_bypass.requests, "get", lambda *_a, **_k: Resp())
    with pytest.raises(RuntimeError, match="ambiguous"):
        parallel_bypass._validate_unique_linkedin_ws_target(
            "ws://127.0.0.1:9222/devtools/page/a", "https://www.linkedin.com/jobs/search/"
        )
