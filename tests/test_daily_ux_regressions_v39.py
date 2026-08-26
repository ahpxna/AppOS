from __future__ import annotations

import pytest


def test_openclaw_user_handoff_opens_one_new_tab_then_focuses_it():
    from services.autofill.autofill_executor_v1 import OpenClawTransport

    class Transport(OpenClawTransport):
        def __init__(self):
            self.opened = False
            self.commands: list[list[str]] = []
        def _tabs(self):
            tabs = [{"tabId": "old", "url": "https://other.example/", "active": True}]
            if self.opened:
                tabs.append({"tabId": "job", "url": "https://jobs.example/123"})
            return tabs
        def _run(self, args, *, json_output=False):
            self.commands.append(args)
            if args[0] == "open":
                self.opened = True
            return ""

    transport = Transport()
    target = transport.open("https://jobs.example/123")
    assert target.target_id == "job"
    assert target.url == "https://jobs.example/123"
    assert transport.commands == [["open", "https://jobs.example/123"], ["focus", "job"]]


def test_openclaw_user_handoff_refuses_ambiguous_new_tabs():
    from services.autofill.autofill_executor_v1 import OpenClawTransport, TransportError

    class Transport(OpenClawTransport):
        def __init__(self): self.opened = False
        def _tabs(self):
            tabs = [{"tabId": "old", "url": "https://other.example/"}]
            if self.opened:
                tabs.extend([
                    {"tabId": "one", "url": "https://jobs.example/123"},
                    {"tabId": "two", "url": "https://popup.example/"},
                ])
            return tabs
        def _run(self, args, *, json_output=False):
            self.opened = True
            return ""

    with pytest.raises(TransportError, match="exactly one tab"):
        Transport().open("https://jobs.example/123")


def test_open_apply_handoff_focuses_existing_exact_job_tab(monkeypatch):
    from services.application_actions import privileged_action_v1 as privileged
    from services.review import review_service_v1 as review

    class Transport:
        def __init__(self): self.focused = []
        def tabs(self): return [{"tabId": "job", "url": "https://jobs.example/123?ref=a"}]
        @staticmethod
        def _stable_id(tab): return str(tab["tabId"])
        def focus(self, target_id):
            self.focused.append(target_id)
            return type("Target", (), {"target_id": target_id, "url": "https://jobs.example/123?ref=a"})()
        def open(self, _url): raise AssertionError("existing exact tab must be focused, not duplicated")

    transport = Transport()
    monkeypatch.setattr(privileged, "_transport", lambda: transport)
    assert review._focus_or_open_exact_job_page("https://jobs.example/123?ref=a") == "job"
    assert transport.focused == ["job"]


def test_nonprivileged_reconciliation_has_one_no_id_close_action():
    from services.telegram import telegram_review_bot_v1 as tg

    class Cur:
        def execute(self, *_args, **_kwargs): pass
        def fetchone(self): return ("source",)

    keyboard = tg._keyboard(Cur(), "review", 7, "reconciliation_required", {})
    assert "I inspected the form" in keyboard
    assert "NOT OCCURRED" not in keyboard
    assert "Reject" not in keyboard


def test_daily_dashboard_refreshes_before_ui_token_expiry():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "services" / "telegram" / "telegram_review_bot_v1.py").read_text()
    assert "interval '20 minutes'" in source
    assert "dispatch_dashboard(conn, token, allowed_user_id, chat_id, force=True)" in source


def test_legal_questions_never_receive_generic_one_tap_choices():
    from services.review import review_service_v1 as review

    class Cur:
        def execute(self, *_args, **_kwargs): pass
        def fetchone(self):
            return ({"question": "Are you legally authorized to work in the United States?"},)

    assert review.question_quick_choices(Cur(), "question") == []


def test_document_review_badge_says_review_not_missing():
    from services.review.ux_policy_v1 import status_badges

    badges = status_badges({"documents": {}, "job": {}, "form": {}}, reviewing_doc="resume")
    assert "Resume 🟡 review" in badges


def test_batch_query_requires_a_live_reviewed_pdf_for_documents():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "services" / "review" / "review_service_v1.py").read_text()
    assert "h.reviewed_artifact_id::text" in source
    assert 'str(status) != "pending" or not reviewed_artifact_id' in source
