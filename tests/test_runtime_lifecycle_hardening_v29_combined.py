from pathlib import Path
from types import SimpleNamespace
import json

ROOT = Path(__file__).resolve().parents[1]


def test_migration_073_adds_autofill_fence_and_auth_recovery_edges():
    sql = (ROOT / "db/migrations/073_runtime_lifecycle_recovery.sql").read_text()
    assert "autofill_executing" in sql
    for edge in (
        "('needs_email_verification', 'needs_account_auth'",
        "('needs_mfa', 'needs_account_auth'",
        "('needs_human_checkpoint', 'needs_mfa'",
        "('needs_human_checkpoint', 'needs_email_verification'",
    ):
        assert edge in sql


def test_company_research_orchestrator_uses_module_entrypoint():
    source = (ROOT / "services/orchestrator/orchestrator_v1.py").read_text()
    assert 'RESEARCH_MODULE = "services.research.company_research_v1"' in source
    assert "run_module(RESEARCH_MODULE" in source
    assert "cwd=REPO_ROOT" in source


def test_direct_file_service_entrypoints_bootstrap_repo_root():
    for rel in (
        "services/research/company_research_v1.py",
        "services/discovery/ats_discovery_v1.py",
        "services/autofill/autofill_agent_v1.py",
        "services/repo-audit/repo_coordinator_v1.py",
    ):
        assert "JOBOS_DIRECT_FILE_BOOTSTRAP" in (ROOT / rel).read_text()


def test_openclaw_gmail_defaults_do_not_use_deprecated_llama_free_model():
    bootstrap = (ROOT / "scripts/openclaw_bootstrap.py").read_text()
    env = (ROOT / ".env.example").read_text()
    assert "llama-3.3-70b-instruct:free" not in bootstrap
    assert "llama-3.3-70b-instruct:free" not in env
    assert '"GMAIL_MODEL": "openrouter/auto"' in bootstrap
    assert "OPENCLAW_GMAIL_MODEL=openrouter/auto" in env


def test_review_hub_maps_executing_and_consumed_capability_as_approved_not_expired():
    source = (ROOT / "services/review/review_service_v1.py").read_text()
    assert "ar.status IN ('approved','executing','consumed')" in source


def test_privileged_request_expires_stale_rows_and_uses_conflict_winner():
    source = (ROOT / "services/application_actions/action_request_v1.py").read_text()
    assert "token_expires_at <= now()" in source
    assert "ON CONFLICT (idempotency_key)" in source
    assert "status IN ('pending','approved','executing')" in source
    assert "Concurrent materializers race safely" in source


def test_screenshot_filename_is_uuid_and_review_context_not_in_authorization_digest():
    action = (ROOT / "services/application_actions/privileged_action_v1.py").read_text()
    request = (ROOT / "services/application_actions/action_request_v1.py").read_text()
    assert "uuid4().hex" in action
    assert '"review_context"' in request and "_authorization_payload" in request


def test_create_account_terms_is_not_peer_option():
    source = (ROOT / "services/application_actions/privileged_action_v1.py").read_text()
    assert "privileged_choose_create_employer_account_path" in source
    assert "create_path_terms_required" in source
    assert "CREATE ACCOUNT path selected" in source


def test_deterministic_autofill_no_longer_runs_fake_mouse():
    source = (ROOT / "services/browser-controller/browser_queue_worker.py").read_text()
    start = source.index("def handle_fill_application_form")
    tail = source[start:]
    assert "Deterministic autofill intentionally does not run synthetic mouse motion" in tail
    assert "Đã thả chuột ma lượn lờ trong lúc Autofill" not in tail


def test_linkedin_helper_ignores_wrong_first_tab_if_any_linkedin_tab_exists():
    source = (ROOT / "services/autofill/parallel_bypass.py").read_text()
    assert "if any(_is_linkedin_url" in source
    assert "_canonical_target_url" in source
    assert "parse_qsl" in source


def test_linkedin_jd_blocker_words_do_not_trigger_frozen_handler_scan():
    from services.discovery.linkedin_discovery_v1 import blocker_safe_agent_response, normalize_jobs

    jd = (
        "Verification Engineer role. Employment verification and background security check are part of onboarding. "
        "Engineers may sign in to internal tooling. " + "Grounded job detail. " * 20
    )
    response = {
        "parsed": {
            "jobs": [{
                "company": "Example Co",
                "title": "Verification Engineer",
                "location": "Remote",
                "work_mode": "remote",
                "url": "https://www.linkedin.com/jobs/view/123456/",
                "jd_text": jd,
            }]
        }
    }
    wrapped = blocker_safe_agent_response(response)
    scan = str(wrapped).casefold()
    json_scan = json.dumps(wrapped, ensure_ascii=False).casefold()
    assert "verification engineer" not in scan
    assert "verification engineer" not in json_scan
    assert "security check" not in scan
    assert "sign in" not in scan
    rows = normalize_jobs(wrapped, 1)
    assert rows[0]["jd_text"] == jd.strip()


def test_linkedin_raw_blocker_report_remains_visible_to_frozen_handler_scan():
    from services.discovery.linkedin_discovery_v1 import blocker_safe_agent_response

    response = {"raw_output": "CAPTCHA checkpoint requires sign in"}
    wrapped = blocker_safe_agent_response(response)
    assert wrapped is response
    assert "captcha" in str(wrapped).casefold()


def test_untrusted_navigation_is_observed_without_mutating_authoritative_state(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    class Transport:
        def _tabs(self):
            return [{"id": "tab", "type": "page"}]
        def _stable_id(self, tab):
            return tab.get("id")

    monkeypatch.setattr(action.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        action,
        "_snapshot",
        lambda _transport, _target: (
            "https://new-ats.example/apply",
            {"snapshot": "Application form"},
            [{"ref": "name", "role": "textbox", "label": "Name"}],
            "f" * 64,
        ),
    )
    monkeypatch.setattr(action, "detect_platform", lambda _url, _snap: "custom")
    monkeypatch.setattr(action, "detect_page_state", lambda _url, _snap, _nodes: ("application_form_ready", {}))
    monkeypatch.setattr(action, "_host_is_allowed", lambda _cur, _url, **_kwargs: False)
    monkeypatch.setattr(
        action,
        "_update_auth_session",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("untrusted observation must not mutate state")),
    )

    result = action._after_navigation(object(), Transport(), "app", "tab", [{"id": "tab", "type": "page"}])
    assert result["state"] == "application_form_ready"
    assert result["followup"] == "trust_domain_required"


def test_untrusted_application_form_materializes_trust_before_autofill(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    created = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    class Conn:
        def cursor(self): return Cur()
        def commit(self): return None

    monkeypatch.setattr(action, "_transport", lambda: object())
    monkeypatch.setattr(
        action,
        "_snapshot",
        lambda _transport, _target: (
            "https://new-ats.example/apply", {"snapshot": "form"}, [], "f" * 64
        ),
    )
    monkeypatch.setattr(action, "_host_is_allowed", lambda _cur, _url, **_kwargs: False)
    monkeypatch.setattr(action, "_capture_review_screenshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        action,
        "create_privileged_request",
        lambda _cur, **kwargs: created.append(kwargs) or "trust-request",
    )
    monkeypatch.setattr(
        action.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("autofill must not run before trust")),
    )

    out = action._post_commit_followup(
        Conn(), "app",
        {"target_id": "tab", "state": "application_form_ready", "followup": "trust_domain_required"},
    )
    assert out == {"kind": "trust_domain", "approval_request_id": "trust-request"}
    assert created[0]["action_type"] == "privileged_trust_external_domain"


def test_login_and_create_with_terms_materializes_login_plus_create_choice_only(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    created = []
    monkeypatch.setattr(action, "_capture_review_screenshot", lambda *_a, **_k: None)

    def account_payload(_cur, _nodes, _url, *, action: str):
        if action == "create_employer_account":
            return ({"control_ref": "create", "consent_blockers": ["Terms"],
                     "consent_items": [{"ref": "terms", "label": "Accept terms"}]}, [])
        return ({"control_ref": "login", "consent_blockers": []}, [])

    monkeypatch.setattr(action, "_account_action_payload", account_payload)
    monkeypatch.setattr(
        action,
        "create_privileged_request",
        lambda _cur, **kwargs: created.append(kwargs) or f"r{len(created)}",
    )
    action._enqueue_state_followup(
        object(), object(), application_id="app", target_id="tab",
        url="https://ats.example/auth", fingerprint="f" * 64,
        nodes=[{"label": "Sign in"}, {"label": "Create account"}], state="needs_account_auth",
    )
    assert [item["action_type"] for item in created] == [
        "privileged_login_employer_account",
        "privileged_choose_create_employer_account_path",
    ]
    assert all(item["action_type"] != "privileged_accept_terms" for item in created)
