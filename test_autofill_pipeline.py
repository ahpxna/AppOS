from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.autofill_session_v1 import AutofillSession, SnapshotState
from services.autofill.autofill_verifier_v1 import verify_actions
from services.autofill.form_inspector_v1 import FormField, QuestionGroup, QuestionOption


def test_static_identity_is_narrow_and_verifiable():
    actions, _ = plan_autofill(
        [FormField("first", "First name", "textbox"), FormField("email", "Email", "textbox")],
        {"personal": {"first_name": "An", "email": "an@example.com"}},
    )
    assert [action.action for action in actions] == ["fill", "fill"]
    result = verify_actions(actions, {"first": "An", "email": "an@example.com"})
    assert result.status == "completed"


def test_immigration_and_unknown_fields_pause():
    actions, _ = plan_autofill(
        [
            FormField("visa", "Will you now or in the future require sponsorship?", "radio"),
            FormField("odd", "Preferred employment category", "textbox"),
        ],
        {},
    )
    assert [action.action for action in actions] == ["pause", "pause"]
    assert verify_actions(actions, {}).status == "needs_review"


def test_confirmed_semantic_answer_selects_only_the_matching_radio_option():
    group = QuestionGroup(
        "Will you now or in the future require sponsorship?", "radiogroup",
        (QuestionOption("yes", "Yes", False), QuestionOption("no", "No", True)),
    )
    actions, _ = plan_autofill(
        [], {}, question_groups=[group],
        approved_sensitive_answers={
            "SPONSORSHIP_NOW_OR_FUTURE": {
                "value": "Yes", "confirmed_at": "2026-08-23", "confirmation_version": 1,
            },
        },
    )
    assert len(actions) == 1
    assert actions[0].action == "check"
    assert actions[0].ref == "yes"


def test_session_rechecks_origin_and_consumes_after_verified_write():
    class FakeTransport:
        def __init__(self):
            self.value = ""
        def current_url(self):
            return "https://jobs.example.com/apply"
        def snapshot(self):
            return {}
        def execute(self, command):
            self.value = command["value"]
    transport = FakeTransport()
    actions, _ = plan_autofill([FormField("first", "First name", "textbox")], {"personal": {"first_name": "An"}})
    session = AutofillSession(
        transport=transport, expected_origin="https://jobs.example.com",
        snapshot_state=lambda: SnapshotState((FormField("first", "First name", "textbox", transport.value),), ()),
        origin_allowed=lambda _url: None,
    )
    consumed = []
    result = session.execute(actions, on_first_verified_write=lambda: consumed.append(True))
    assert result.status == "completed"
    assert consumed == [True]
