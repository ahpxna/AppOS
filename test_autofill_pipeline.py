from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.autofill_verifier_v1 import verify_actions
from services.autofill.form_inspector_v1 import FormField


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
