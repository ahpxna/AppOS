import tempfile
from pathlib import Path

from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.autofill_executor_v1 import BrowserTarget, OpenClawTransport
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


def test_session_pins_target_rematches_after_write_and_journals_it():
    class FakeTransport:
        def __init__(self):
            self.value = ""
            self.snapshots = 0
        def resolve_target(self):
            return BrowserTarget("tab-1", "https://jobs.example.com/apply")
        def current_url(self, target_id):
            assert target_id == "tab-1"
            return "https://jobs.example.com/apply"
        def snapshot(self, target_id):
            return {}
        def execute(self, target_id, command):
            assert target_id == "tab-1"
            self.value = command["value"]
    transport = FakeTransport()
    actions, _ = plan_autofill([FormField("first", "First name", "textbox")], {"personal": {"first_name": "An"}})
    journal = []
    session = AutofillSession(
        transport=transport, expected_origin="https://jobs.example.com",
        snapshot_state=lambda _target: SnapshotState((FormField("first", "First name", "textbox", transport.value),), ()),
        origin_allowed=lambda _url: None,
        begin_execution=lambda target: journal.append(("begin", target)),
        before_action=lambda action, target: journal.append(("before", action.ref, target)) or "journal-1",
        after_verified=lambda action, target, item: journal.append(("verified", action.ref, target, item)),
        after_failed=lambda action, target, item: journal.append(("failed", action.ref, target, item)),
    )
    result = session.execute(lambda _state: actions)
    assert result.status == "completed"
    assert journal == [
        ("begin", "tab-1"), ("before", "first", "tab-1"),
        ("verified", "first", "tab-1", "journal-1"),
    ]


def test_openclaw_fill_uses_documented_fields_payload_and_pinned_target():
    class CaptureTransport(OpenClawTransport):
        def __init__(self):
            super().__init__(binary="openclaw", profile="remote")
            self.calls = []
        def _run(self, args, *, json_output=False):
            self.calls.append((args, json_output))
            return "{}"

    transport = CaptureTransport()
    transport.execute("target-7", {"action": "fill", "target": "e12", "value": "Ada"})
    assert transport.calls == [
        (["fill", "--fields", '[{"ref":"e12","value":"Ada"}]', "--target-id", "target-7"], False)
    ]


def test_openclaw_upload_stages_managed_copy_before_ref_upload():
    class CaptureTransport(OpenClawTransport):
        def __init__(self, uploads_dir):
            super().__init__(binary="openclaw", profile="remote", uploads_dir=uploads_dir)
            self.calls = []
        def _run(self, args, *, json_output=False):
            self.calls.append((args, json_output))
            return "{}"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "resume.pdf"
        source.write_bytes(b"approved resume")
        uploads = root / "openclaw-uploads"
        transport = CaptureTransport(uploads)
        transport.execute("target-8", {"action": "upload", "target": "e15", "value": str(source)})
        staged = next(uploads.iterdir())
        assert staged.read_bytes() == b"approved resume"
        assert transport.calls == [
            (["upload", str(staged), "--ref", "e15", "--target-id", "target-8"], False)
        ]
