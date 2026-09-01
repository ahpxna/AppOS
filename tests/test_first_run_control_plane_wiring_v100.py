from __future__ import annotations

from pathlib import Path
from decimal import Decimal

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_orchestrator_claims_only_steps_with_an_advance_handler():
    from services.orchestrator import orchestrator_v1 as orchestrator

    assert set(orchestrator.ORCHESTRATOR_OWNED_STEPS) == {
        "intake", "screened", "fit_analyzed", "docs_generated",
    }
    assert "application_form_ready" not in orchestrator.ORCHESTRATOR_OWNED_STEPS
    assert "autofill_executing" not in orchestrator.ORCHESTRATOR_OWNED_STEPS
    source = (ROOT / "services/orchestrator/orchestrator_v1.py").read_text()
    claim = source[source.index("def claim_application"):source.index("def release_application_claim")]
    assert "a.current_step = ANY(%s)" in claim
    assert "ps.requires_human=false" not in claim


def test_unknown_apply_landing_materializes_exact_read_only_retry(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    created: list[dict] = []
    monkeypatch.setattr(action, "_capture_review_screenshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        action,
        "create_privileged_request",
        lambda _cur, **kwargs: created.append(kwargs) or "request-id",
    )

    action._enqueue_state_followup(
        object(), object(), application_id="app", target_id="tab",
        url="https://ats.example/apply", fingerprint="f" * 64,
        nodes=[], state="unknown",
    )

    assert len(created) == 1
    assert created[0]["action_type"] == "privileged_auth_manual_retry"
    assert created[0]["payload"]["target_id"] == "tab"
    assert "will not be replayed" in created[0]["summary"]


def test_auth_watcher_turns_untrusted_same_tab_sso_redirect_into_trust_gate(monkeypatch):
    from services.auth import browser_state_watcher_v1 as watcher

    class Cur:
        def __init__(self, kind: str):
            self.kind = kind

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            if self.kind == "seed":
                return [("app", "needs_account_auth", "https://ats.example/login", "old-fp",
                         "needs_account_auth", {"target_id": "tab"},
                         "https://ats.example/jobs/1", "jd-1", "Example", "Engineer",
                         "https://ats.example/jobs/1", "jd-1")]
            return []

        def fetchone(self):
            # No already-live trust capability for the redirected exact target.
            return None

    class Conn:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            return Cur("seed" if self.calls == 1 else "check")

        def rollback(self):
            return None

        def commit(self):
            return None

    followups: list[dict] = []
    monkeypatch.setattr(watcher, "_transport", lambda: object())
    monkeypatch.setattr(
        watcher, "_snapshot",
        lambda *_a: ("https://login.microsoftonline.com/oauth2/authorize",
                     {"snapshot": "Sign in"}, [{"role": "textbox", "label": "Email"}], "new-fp"),
    )
    monkeypatch.setattr(watcher, "detect_page_state", lambda *_a: ("needs_manual_sso", {}))
    monkeypatch.setattr(watcher, "detect_platform", lambda *_a: "custom")
    monkeypatch.setattr(watcher, "_host_is_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        watcher, "_post_commit_followup",
        lambda _conn, _app, result: followups.append(result) or {"kind": "trust_domain"},
    )

    changed = watcher.observe_once(Conn())
    assert followups[0]["followup"] == "trust_domain_required"
    assert followups[0]["target_id"] == "tab"
    assert changed[0]["trust_required"] is True


def test_send_message_capability_cannot_be_created_before_truth_qa():
    from services.messaging.message_reply_v1 import create_send_message_approval

    class Cur:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return ("revise",)

    with pytest.raises(RuntimeError, match="truth-QA-passed"):
        create_send_message_approval(
            Cur(), reply_id="reply", thread_id="thread", application_id="app",
            company="Example", subject="Re: Role", body_text="Thanks.",
            evidence_map={}, asset_ids_used=[],
        )


def test_send_message_human_decision_updates_domain_state():
    from services.approval.approval_service_v1 import _resolve_send_message_decision

    class Cur:
        def __init__(self):
            self.sql: list[str] = []

        def execute(self, sql, *_args):
            self.sql.append(" ".join(sql.split()))

        def fetchone(self):
            return ("reply",)

    approved = Cur()
    _resolve_send_message_decision(approved, payload={"drafted_reply_id": "reply"}, approved=True)
    assert any("SET approved=true" in sql and "qa_status='pass'" in sql for sql in approved.sql)

    denied = Cur()
    _resolve_send_message_decision(denied, payload={"drafted_reply_id": "reply"}, approved=False)
    assert any("SET approved=false" in sql for sql in denied.sql)


def test_reply_send_gate_is_created_after_verify_not_after_draft():
    source = (ROOT / "services/messaging/message_reply_v1.py").read_text()
    draft = source[source.index("def cmd_draft"):source.index("def cmd_verify")]
    verify = source[source.index("def cmd_verify"):source.index("# ---------------------------------------------------------------- inbox")]
    assert "create_send_message_approval(" not in draft
    assert 'result["qa_status"] == "pass"' in verify
    assert "create_send_message_approval(" in verify
    assert "ar.status='denied'" in source


def test_message_worker_never_auto_drafts_unlinked_or_human_attention_threads():
    source = (ROOT / "services/messaging/message_pipeline_worker_v1.py").read_text()
    assert "v.linked_application_id IS NOT NULL" in source
    assert "coalesce(mt.needs_user_attention,false)=false" in source
    assert "dr.superseded_at IS NULL" in source


def test_reply_authority_is_bound_to_latest_inbound_message():
    migration = (ROOT / "db/migrations/103_message_reply_conversation_binding.sql").read_text()
    approval = (ROOT / "services/approval/approval_service_v1.py").read_text()
    reply = (ROOT / "services/messaging/message_reply_v1.py").read_text()
    assert "jobos_supersede_reply_on_new_inbound" in migration
    assert "v_latest_inbound_id IS DISTINCT FROM NEW.id" in migration
    assert '"in_reply_to": reply["in_reply_to"]' in approval
    assert "Recruiter thread changed after this reply was drafted" in approval
    assert "message_threads.needs_user_attention OR" in reply


def test_component_run_provenance_is_not_hardcoded_to_ollama():
    fit = (ROOT / "services/job-analysis/analyze_job_fit_v1.py").read_text()
    docs = (ROOT / "services/document-generation/generate_documents_v1.py").read_text()
    reply = (ROOT / "services/messaging/message_reply_v1.py").read_text()
    assert "provider=llm_config.provider" in fit
    assert "provider=resolved_llm.provider" in docs
    assert "llm_config.provider, llm_config.model" in reply


def test_auth_watcher_requires_application_identity_on_form_landing():
    from services.auth.browser_state_watcher_v1 import _application_form_identity_is_grounded

    assert _application_form_identity_is_grounded(
        live_url="https://ats.example/apply/123", snapshot={"snapshot": "unrelated"},
        job_url="https://ats.example/apply/123", company="Acme", job_title="Platform Engineer",
    )
    assert _application_form_identity_is_grounded(
        live_url="https://ats.example/session/redirected",
        snapshot={"snapshot": "Acme — Platform Engineer application"},
        job_url="https://linkedin.example/jobs/123", company="Acme", job_title="Platform Engineer",
    )
    assert not _application_form_identity_is_grounded(
        live_url="https://ats.example/session/redirected",
        snapshot={"snapshot": "Other Co — Platform Engineer application"},
        job_url="https://linkedin.example/jobs/123", company="Acme", job_title="Platform Engineer",
    )


def test_auth_session_schema_binds_job_url_and_jd_hash():
    migration = (ROOT / "db/migrations/104_auth_session_application_identity.sql").read_text()
    runtime = (ROOT / "services/application_actions/privileged_action_v1.py").read_text()
    assert "binding_job_url" in migration and "binding_jd_hash" in migration
    assert "auth session application/JD identity mismatch" in migration
    assert "binding_job_url,binding_jd_hash" in runtime


def test_profile_embedding_dimension_env_is_bounded_safe_parse():
    for path in (
        ROOT / "services/profile-ingestion/embed_profile_chunks.py",
        ROOT / "services/profile-ingestion/profile_retrieval_api.py",
    ):
        source = path.read_text()
        assert 'env_int("PROFILE_EMBED_DIM", 768, minimum=1, maximum=4096)' in source
        assert 'int(os.getenv("PROFILE_EMBED_DIM"' not in source


def test_prompt_injection_classification_cannot_become_auto_draft_after_ack():
    source = (ROOT / "services/messaging/message_reply_v1.py").read_text()
    block = source[source.index("contains_ai_instructions ="):source.index("if args.apply:", source.index("contains_ai_instructions ="))]
    assert 'label = "unclear"' in block
    assert "meta = labels[label]" in block


def test_entrypoint_fallback_uses_completed_apply_result_not_ambient_focus():
    source = (ROOT / "services/review/review_service_v1.py").read_text()
    sync = source[source.index("def sync_workflow_followup_required"):source.index("def ensure_reconciliation_review")]
    assert "application_entrypoint_ready" in sync
    assert "privileged_action_executions" in sync
    assert "privileged_begin_application" in sync
    assert "nav.result_json->>'target_id'" in sync


def test_checkpoint_to_login_is_recovery_in_schema_and_runtime():
    migration = (ROOT / "db/migrations/101_checkpoint_account_auth_transition_authority.sql").read_text()
    runtime = (ROOT / "services/application_actions/privileged_action_v1.py").read_text()
    assert "from_step='needs_human_checkpoint'" in migration
    assert "to_step='needs_account_auth'" in migration
    assert "transition_kind='recovery'" in migration
    assert '("needs_human_checkpoint", "needs_account_auth")' in runtime


def test_runtime_ready_requires_gateway_and_cdp_health():
    from scripts.jobos_runtime_supervisor import runtime_state_ready

    base = {
        "database_health": {"available": True, "error": None},
        "browser_runtime_health": {"available": False, "error": "CDP unavailable"},
    }
    assert runtime_state_ready(base, running=True, state_fresh=True, required_failures=[]) is False
    base["browser_runtime_health"] = {"available": True, "error": None}
    assert runtime_state_ready(base, running=True, state_fresh=True, required_failures=[]) is True


def test_runtime_start_proves_latest_migration_before_spawning(monkeypatch, tmp_path, capsys):
    import scripts.jobos_runtime_supervisor as supervisor

    monkeypatch.setattr(supervisor, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(supervisor, "SUPERVISOR_PID", tmp_path / "run" / "supervisor.pid")
    monkeypatch.setattr(supervisor, "STATE_FILE", tmp_path / "run" / "runtime.json")
    monkeypatch.setattr(supervisor, "load_repo_env", lambda: None)
    monkeypatch.setattr(supervisor, "_read_pid", lambda: None)
    monkeypatch.setattr(supervisor, "_is_supervisor_process", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_start_infra", lambda: None)
    monkeypatch.setattr(
        supervisor, "_preflight_database_contract",
        lambda **_kwargs: (False, "migration_not_current=101_example.sql"),
    )
    spawned: list[object] = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_a, **_k: spawned.append(object()))

    assert supervisor.start() == 1
    assert spawned == []
    assert "workers were not started" in capsys.readouterr().err


def test_application_bound_llm_reuses_exact_completed_response(monkeypatch):
    from services.common import llm_cost_accounting_v1 as accounting
    from services.common import llm_gateway

    monkeypatch.setenv("JOBOS_APPLICATION_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(
        llm_gateway, "resolve_config",
        lambda **_kwargs: llm_gateway.LLMConfig(
            role="docgen", backend="ollama", model="model", base_url="http://127.0.0.1:11434",
            api_key=None, api_style="ollama", provider="ollama",
        ),
    )
    monkeypatch.setattr(
        accounting, "lookup_completed_call",
        lambda **_kwargs: accounting.CachedCall(
            response_json={"text": "cached exact output"}, resolved_model="model",
            input_tokens=10, output_tokens=4, cost_usd=Decimal("0"), request_id="req-1",
        ),
    )
    monkeypatch.setattr(
        llm_gateway, "_post_json",
        lambda *_a, **_k: pytest.fail("provider must not be called for an exact completed response"),
    )

    result = llm_gateway.generate_result(role="docgen", prompt="same exact prompt")
    assert result.text == "cached exact output"
    assert result.request_id == "req-1"


def test_llm_response_replay_migration_and_status_surface_exist():
    migration = (ROOT / "db/migrations/102_llm_exact_response_replay.sql").read_text()
    accounting = (ROOT / "services/common/llm_cost_accounting_v1.py").read_text()
    status = (ROOT / "scripts/jobos.py").read_text()
    assert "ADD COLUMN IF NOT EXISTS response_json jsonb" in migration
    assert "pg_advisory_xact_lock" in accounting
    assert "cached_response_json" in accounting
    assert "llm_calls_needing_reconciliation" in status
