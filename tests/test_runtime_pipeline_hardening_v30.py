from __future__ import annotations


def test_repair_child_binds_exact_parent(monkeypatch):
    from services.approval import approval_service_v1 as approval
    from services.application_actions import action_request_v1

    captured = []
    monkeypatch.setattr(
        action_request_v1,
        "create_privileged_request",
        lambda cur, **kwargs: captured.append(kwargs) or "child-1",
    )
    payload = {
        "delegated_upload_packages": [{
            "field_ref": "resume", "document_type": "resume", "artifact_id": "art",
            "sha256": "a" * 64, "autofill_plan_key": "plan", "filename": "resume.pdf",
        }]
    }
    ids = approval._repair_delegated_children_for_parent(
        object(), application_id="app", parent_request_id="parent-B", payload=payload,
    )
    assert ids == ["child-1"]
    child_payload = captured[0]["payload"]
    assert child_payload["parent_approval_request_id"] == "parent-B"
    assert child_payload["delegated_to_autofill"] is True


def test_queue_does_not_reuse_child_from_old_parent(monkeypatch):
    from services.approval import approval_service_v1 as approval

    expected = [{"field_ref": "resume", "document_type": "resume", "artifact_id": "art", "sha256": "a" * 64}]
    parent = {"expected_upload_capabilities": expected, "delegated_upload_packages": []}

    class Cur:
        def __init__(self):
            self.sql = ""; self.params = (); self.rowcount = 0
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split()); self.params = params or (); self.rowcount = 0
        def fetchone(self):
            if "type='autofill_form'" in self.sql:
                return ("parent-B", parent, "approved")
            return None
        def fetchall(self):
            # Real SQL filters parent_approval_request_id=parent-B. Simulate an
            # old child belonging to A as absent rather than reusable.
            if "LEFT JOIN privileged_action_executions" in self.sql:
                assert self.params[1] == "parent-B"
                return []
            return []

    monkeypatch.setattr(approval, "_repair_delegated_children_for_parent", lambda *_a, **_k: [])
    monkeypatch.setattr(approval, "assert_binding_matches", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not queue")))
    assert approval.queue_ready_autofill_for_plan(Cur(), application_id="app", plan_key="plan", actor="test") is False


def test_consent_classifier_excludes_policy_and_marketing():
    from services.application_actions.privileged_action_v1 import _consent_items

    items = _consent_items([
        {"ref": "p", "role": "button", "label": "Privacy Policy", "selected": None},
        {"ref": "m", "role": "checkbox", "label": "I consent to receive marketing emails", "selected": False},
        {"ref": "c", "role": "checkbox", "label": "I agree to the privacy terms", "selected": False, "required": True},
        {"ref": "a", "role": "button", "label": "Accept and continue", "selected": None},
    ])
    assert [item["ref"] for item in items] == ["c", "a"]


def test_button_consent_needs_more_than_page_change():
    from services.application_actions.privileged_action_v1 import _consent_effect_verified
    approved = [{"ref": "a", "role": "button", "label": "Accept and continue", "selected": None}]
    assert not _consent_effect_verified(approved, [], page_changed=True, observed_state="unknown")
    assert _consent_effect_verified(approved, [], page_changed=True, observed_state="needs_account_auth")


def test_upload_reconciliation_requires_exact_filename(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    monkeypatch.setattr(action, "parse_snapshot", lambda snap: snap["nodes"])
    old = {"nodes": [{"ref": "u", "value": r"C:\\fakepath\\old_resume.pdf"}], "snapshot": "old_resume.pdf"}
    exact = {"nodes": [{"ref": "u", "value": r"C:\\fakepath\\resume_final.pdf"}], "snapshot": "resume_final.pdf"}
    assert not action._upload_effect_verified(
        before_snapshot={"snapshot": ""}, after_snapshot=old,
        field_ref="u", filename="resume_final.pdf", allow_text_fallback=False,
    )
    assert action._upload_effect_verified(
        before_snapshot={"snapshot": ""}, after_snapshot=exact,
        field_ref="u", filename="resume_final.pdf", allow_text_fallback=False,
    )


def test_reconciliation_uses_bound_target_not_focused_tab(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    class Cur:
        def __init__(self): self.sql = ""
        def execute(self, sql, params=None): self.sql = " ".join(sql.split())
        def fetchone(self):
            if "FROM approval_requests ar" in self.sql:
                return ({"target_id": "A", "expected_url": "https://ats.example/app/a", "expected_origin": "https://ats.example"}, {})
            return None

    class Transport:
        def resolve_target(self):
            raise AssertionError("focused tab must never be consulted")
        def _tabs(self): return []

    monkeypatch.setattr(action, "_transport", lambda: Transport())
    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        "https://ats.example/app/a", {"snapshot": "application"}, [], "f" * 64
    ) if tid == "A" else (_ for _ in ()).throw(AssertionError("wrong target")))
    monkeypatch.setattr(action, "_host_is_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(action, "detect_platform", lambda *_a: "custom")
    monkeypatch.setattr(action, "detect_page_state", lambda *_a: ("application_form_ready", {}))
    seen = []
    monkeypatch.setattr(action, "_update_auth_session", lambda _cur, **kwargs: seen.append(kwargs))

    result = action.reconcile_observed_privileged_effect(
        Cur(), application_id="app-A", approval_request_id="req", action_type="privileged_login_employer_account",
    )
    assert result["target_id"] == "A"
    assert seen[0]["application_id"] == "app-A"


def test_multiple_new_tabs_become_human_ambiguity(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    before = [{"id": "source", "type": "page"}]
    after = before + [
        {"id": "ats", "type": "page", "url": "https://ats.example/app"},
        {"id": "help", "type": "page", "url": "https://ats.example/help"},
    ]
    class Transport:
        def _tabs(self): return after
        def _stable_id(self, tab): return tab.get("id")
    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        next(t["url"] for t in after if t.get("id") == tid), {"snapshot": tid}, [], (tid[0] * 64)
    ))
    monkeypatch.setattr(action, "detect_page_state", lambda url, *_a: ("application_form_ready", {}) if url.endswith("/app") else ("unknown", {}))
    target, candidates = action._select_after_navigation_target(Transport(), "source", before)
    assert target == "ats"
    assert candidates == []

    monkeypatch.setattr(action, "detect_page_state", lambda *_a: ("application_form_ready", {}))
    target, candidates = action._select_after_navigation_target(Transport(), "source", before)
    assert target is None
    assert {item["target_id"] for item in candidates} == {"ats", "help"}


def test_changed_source_beats_new_auth_popup(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    before = [{"id": "source", "type": "page", "url": "https://linkedin.example/job/1"}]
    after = [
        {"id": "source", "type": "page", "url": "https://ats.example/apply/1"},
        {"id": "popup", "type": "page", "url": "https://idp.example/login", "openerId": "source"},
    ]

    class Transport:
        def _tabs(self): return after
        def _stable_id(self, tab): return tab.get("id")

    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        next(t["url"] for t in after if t["id"] == tid), {"snapshot": tid}, [], tid * 8,
    ))
    monkeypatch.setattr(action, "detect_page_state", lambda url, *_a: (
        "application_form_ready" if "ats.example" in url else "needs_account_auth", {}
    ))
    target, candidates = action._select_after_navigation_target(Transport(), "source", before)
    assert target == "source"
    assert candidates == []


def test_single_unclassified_popup_is_not_accepted_as_apply_target(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    before = [{"id": "source", "type": "page", "url": "https://linkedin.example/job/1"}]
    after = before + [{"id": "popup", "type": "page", "url": "https://help.example/article", "openerId": "source"}]

    class Transport:
        def _tabs(self): return after
        def _stable_id(self, tab): return tab.get("id")

    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        next(t["url"] for t in after if t["id"] == tid), {"snapshot": tid}, [], tid * 8,
    ))
    monkeypatch.setattr(action, "detect_page_state", lambda *_a: ("unknown", {}))
    target, candidates = action._select_after_navigation_target(Transport(), "source", before)
    assert target is None
    assert candidates[0]["target_id"] == "popup"


def test_changed_source_beats_multiple_new_popups(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    before = [{"id": "source", "type": "page", "url": "https://linkedin.example/job/1"}]
    after = [
        {"id": "source", "type": "page", "url": "https://ats.example/apply/1"},
        {"id": "login", "type": "page", "url": "https://idp.example/login", "openerId": "source"},
        {"id": "help", "type": "page", "url": "https://help.example/article"},
    ]

    class Transport:
        def _tabs(self): return after
        def _stable_id(self, tab): return tab.get("id")

    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        next(t["url"] for t in after if t["id"] == tid), {"snapshot": tid}, [], tid * 8,
    ))
    monkeypatch.setattr(action, "detect_page_state", lambda url, *_a: (
        "application_form_ready" if "ats.example" in url else "needs_account_auth", {}
    ))
    target, candidates = action._select_after_navigation_target(Transport(), "source", before)
    assert target == "source"
    assert candidates == []


def test_reconciliation_discovers_unique_ats_handoff_without_browser_focus(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    tabs = [
        {"id": "source", "type": "page", "url": "https://linkedin.example/job/1"},
        {"id": "ats", "type": "page", "url": "https://ats.example/apply/1", "openerId": "source"},
    ]

    class Transport:
        def resolve_target(self):
            raise AssertionError("reconciliation must never use browser focus")
        def _tabs(self): return tabs
        def _stable_id(self, tab): return tab.get("id")

    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        next(t["url"] for t in tabs if t["id"] == tid), {"snapshot": tid}, [], tid * 8,
    ))
    monkeypatch.setattr(action, "detect_page_state", lambda url, *_a: (
        "application_form_ready" if "ats.example" in url else "unknown", {}
    ))
    result = action._reconciliation_target_snapshot(
        Transport(),
        {"target_id": "source", "expected_url": "https://linkedin.example/job/1", "expected_origin": "https://linkedin.example"},
        allow_handoff_discovery=True, pre_io_target_ids={"source"},
    )
    assert result[0] == "ats"
    assert result[1] == "https://ats.example/apply/1"


def test_reconciliation_refuses_ambiguous_ats_handoff(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    tabs = [
        {"id": "source", "type": "page", "url": "https://linkedin.example/job/1"},
        {"id": "ats-a", "type": "page", "url": "https://ats-a.example/apply", "openerId": "source"},
        {"id": "ats-b", "type": "page", "url": "https://ats-b.example/apply", "openerId": "source"},
    ]

    class Transport:
        def _tabs(self): return tabs
        def _stable_id(self, tab): return tab.get("id")

    monkeypatch.setattr(action, "_snapshot", lambda _t, tid: (
        next(t["url"] for t in tabs if t["id"] == tid), {"snapshot": tid}, [], tid * 8,
    ))
    monkeypatch.setattr(action, "detect_page_state", lambda url, *_a: (
        "application_form_ready" if "ats-" in url else "unknown", {}
    ))
    try:
        action._reconciliation_target_snapshot(
            Transport(),
            {"target_id": "source", "expected_url": "https://linkedin.example/job/1", "expected_origin": "https://linkedin.example"},
            allow_handoff_discovery=True, pre_io_target_ids={"source"},
        )
    except action.PrivilegedActionError as exc:
        assert "unique resulting Apply target" in str(exc)
    else:
        raise AssertionError("ambiguous handoff must remain in reconciliation")


def test_security_check_application_question_is_not_checkpoint():
    from services.application_actions.privileged_action_v1 import detect_page_state
    nodes = [
        {"ref": "first", "role": "textbox", "label": "First name"},
        {"ref": "last", "role": "textbox", "label": "Last name"},
        {"ref": "q", "role": "radio", "label": "Are you willing to undergo a background security check?"},
        {"ref": "submit", "role": "button", "label": "Submit application"},
    ]
    state, _ = detect_page_state("https://ats.example/app", {"snapshot": "background security check"}, nodes)
    assert state == "application_form_ready"


def test_general_employer_trust_is_application_scoped_source_contract():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "services/application_actions/privileged_action_v1.py").read_text()
    assert "purpose='employer_handoff'" in source
    assert "JOBOS_EMPLOYER_TRUST_TTL_MINUTES" in source
    # Existing allow-list reads remain for administrator-pretrusted ATS domains,
    # but human trust execution no longer inserts employer domains globally.
    branch = source.split('if payload.get("trust_source") == "gmail_magic_link":', 1)[1]
    assert "INSERT INTO allowed_domains(domain, category, enabled)" not in branch


def test_upload_reconciliation_requires_bound_page_fingerprint(monkeypatch):
    from services.application_actions import privileged_action_v1 as action

    class Transport:
        def _tabs(self):
            return []

    monkeypatch.setattr(action, "_snapshot", lambda _transport, _target_id: (
        "https://ats.example/app/a", {"snapshot": "- form Application"}, [], "new-fingerprint",
    ))
    payload = {
        "target_id": "A",
        "expected_url": "https://ats.example/app/a",
        "expected_origin": "https://ats.example",
        "expected_page_fingerprint": "approved-fingerprint",
    }
    try:
        action._reconciliation_target_snapshot(Transport(), payload, require_exact_page=True)
    except action.PrivilegedActionError as exc:
        assert "fingerprint" in str(exc).casefold()
    else:
        raise AssertionError("reconciliation must refuse a changed approval-bound page fingerprint")
