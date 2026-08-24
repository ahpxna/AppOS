import tempfile
from pathlib import Path

from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.autofill_executor_v1 import BrowserTarget, OpenClawTransport
from services.autofill.autofill_session_v1 import AutofillSession, SessionError, SnapshotState
from services.autofill.autofill_verifier_v1 import verify_actions
from services.autofill.form_inspector_v1 import FormField, QuestionGroup, QuestionOption
from services.common.autofill_identity import (
    canonical_page_url,
    autofill_input_hash,
)

def test_autofill_input_hash_binds_remembered_answers():
    common = dict(
        profile={
            "personal": {
                "first_name": "An",
            },
        },
        sensitive_answers={},
        document_sha256="a" * 64,
        artifact_sha256=None,
        page_url="https://jobs.example.com/apply?job=123",
        page_fingerprint_sha256="b" * 64,
    )

    blue = autofill_input_hash(
        remembered_answers={
            "favorite color": {
                "value": "Blue",
                "answer_kind": "text",
            },
        },
        **common,
    )

    red = autofill_input_hash(
        remembered_answers={
            "favorite color": {
                "value": "Red",
                "answer_kind": "text",
            },
        },
        **common,
    )

    assert blue != red

def test_static_identity_is_narrow_and_verifiable():
    actions, _ = plan_autofill(
        [FormField("first", "First name", "textbox"), FormField("email", "Email", "textbox")],
        {"personal": {"first_name": "An", "email": "an@example.com"}},
    )
    assert [action.action for action in actions] == ["fill", "fill"]
    result = verify_actions(actions, {"first": "An", "email": "an@example.com"})
    assert result.status == "completed"


def test_already_correct_static_value_is_verified_without_a_browser_write():
    actions, _ = plan_autofill(
        [FormField("phone", "Phone", "textbox", "(609) 555-1234")],
        {"personal": {"phone": "6095551234"}},
    )
    assert actions[0].action == "verify"


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


def test_stem_question_requires_its_own_confirmed_semantic_answer():
    group = QuestionGroup(
        "Will you require a STEM OPT extension?", "radiogroup",
        (QuestionOption("yes", "Yes", False), QuestionOption("no", "No", True)),
    )
    paused, _ = plan_autofill([], {}, question_groups=[group], approved_sensitive_answers={})
    assert paused[0].action == "pause"
    actions, _ = plan_autofill(
        [], {}, question_groups=[group],
        approved_sensitive_answers={
            "WILL_REQUIRE_STEM_EXTENSION": {
                "value": "Yes", "confirmed_at": "2026-08-23", "confirmation_version": 1,
            },
        },
    )
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
        expected_initial_url="https://jobs.example.com/apply", expected_page_fingerprint="fingerprint",
        snapshot_state=lambda _target: SnapshotState((FormField("first", "First name", "textbox", transport.value),), (), "fingerprint"),
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


def test_session_verifies_re_rendered_ref_and_normalized_phone():
    class FakeTransport:
        def __init__(self): self.value = ""
        def resolve_target(self): return BrowserTarget("tab-1", "https://jobs.example.com/apply")
        def current_url(self, _target): return "https://jobs.example.com/apply"
        def snapshot(self, _target): return {}
        def execute(self, _target, command): self.value = command["value"]
    transport = FakeTransport()
    actions, _ = plan_autofill([FormField("old-ref", "Phone", "textbox")], {"personal": {"phone": "6095551234"}})
    session = AutofillSession(
        transport=transport, expected_origin="https://jobs.example.com", expected_initial_url="https://jobs.example.com/apply",
        expected_page_fingerprint="fingerprint",
        snapshot_state=lambda _target: SnapshotState((FormField("new-ref", "Phone", "textbox", "(609) 555-1234"),), (), "fingerprint"),
        origin_allowed=lambda _url: None, begin_execution=lambda _target: None,
        before_action=lambda _action, _target: "journal", after_verified=lambda *_args: None,
        after_failed=lambda *_args: None,
    )
    assert session.execute(lambda _state: actions).status == "completed"


def test_session_refuses_same_origin_but_different_approved_job_page():
    class FakeTransport:
        def resolve_target(self): return BrowserTarget("tab-1", "https://jobs.example.com/job-B/apply")
        def current_url(self, _target): return "https://jobs.example.com/job-B/apply"
        def snapshot(self, _target): return {}
        def execute(self, _target, _command): raise AssertionError("must not write")
    session = AutofillSession(
        transport=FakeTransport(), expected_origin="https://jobs.example.com",
        expected_initial_url="https://jobs.example.com/job-A/apply", expected_page_fingerprint="fingerprint",
        snapshot_state=lambda _target: SnapshotState((), (), "fingerprint"), origin_allowed=lambda _url: None,
        begin_execution=lambda _target: raise_error("must not begin"),
        before_action=lambda *_args: raise_error("must not journal"),
        after_verified=lambda *_args: None, after_failed=lambda *_args: None,
    )
    try:
        session.execute(lambda _state: [])
    except SessionError:
        return
    raise AssertionError("same-origin, different-job page was accepted")


def test_page_identity_preserves_job_query_parameters_but_ignores_fragment():
    assert canonical_page_url("https://jobs.example.com/apply?job=123#section") == \
           canonical_page_url("https://jobs.example.com/apply?job=123#other")
    assert canonical_page_url("https://jobs.example.com/apply?job=123") != \
           canonical_page_url("https://jobs.example.com/apply?job=456")


def test_truncated_initial_snapshot_never_begins_execution():
    class FakeTransport:
        def resolve_target(self): return BrowserTarget("tab-1", "https://jobs.example.com/apply")
        def current_url(self, _target): return "https://jobs.example.com/apply"
        def snapshot(self, _target): return {}
        def execute(self, _target, _command): raise AssertionError("must not write")
    session = AutofillSession(
        transport=FakeTransport(), expected_origin="https://jobs.example.com",
        expected_initial_url="https://jobs.example.com/apply", expected_page_fingerprint="fingerprint",
        snapshot_state=lambda _target: SnapshotState((), (), "fingerprint", True),
        origin_allowed=lambda _url: None, begin_execution=lambda _target: raise_error("must not begin"),
        before_action=lambda *_args: raise_error("must not journal"),
        after_verified=lambda *_args: None, after_failed=lambda *_args: None,
    )
    try:
        session.execute(lambda _state: [])
    except SessionError:
        return
    raise AssertionError("truncated snapshot was accepted")


def raise_error(message):
    raise AssertionError(message)


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
        def __init__(self, uploads_dir, approved_hashes):
            super().__init__(binary="openclaw", profile="remote", uploads_dir=uploads_dir,
                             approved_upload_hashes=approved_hashes)
            self.calls = []
        def _run(self, args, *, json_output=False):
            self.calls.append((args, json_output))
            return "{}"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "resume.pdf"
        source.write_bytes(b"approved resume")
        uploads = root / "openclaw-uploads"
        digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        transport = CaptureTransport(uploads, {str(source.resolve()): digest})
        transport.execute("target-8", {"action": "upload", "target": "e15", "value": str(source)})
        staged = next(uploads.iterdir())
        assert staged.read_bytes() == b"approved resume"
        assert transport.calls == [
            (["upload", str(staged), "--ref", "e15", "--target-id", "target-8"], False)
        ]


def test_openclaw_upload_refuses_bytes_changed_after_approval():
    class CaptureTransport(OpenClawTransport):
        def __init__(self, uploads_dir, approved_hashes):
            super().__init__(binary="openclaw", profile="remote", uploads_dir=uploads_dir,
                             approved_upload_hashes=approved_hashes)
        def _run(self, args, *, json_output=False):
            raise AssertionError("OpenClaw must not receive a changed artifact")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "resume.pdf"
        source.write_bytes(b"approved resume")
        digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        transport = CaptureTransport(root / "uploads", {str(source.resolve()): digest})
        source.write_bytes(b"changed after approval")
        try:
            transport.execute("target-8", {"action": "upload", "target": "e15", "value": str(source)})
        except Exception as exc:
            assert "changed after preflight" in str(exc)
            return
        raise AssertionError("changed artifact bytes were uploaded")

def test_openclaw_upload_requires_an_exact_bound_hash_in_production_mode():
    class CaptureTransport(OpenClawTransport):
        def _run(self, args, *, json_output=False):
            raise AssertionError("OpenClaw must not receive an unbound artifact")

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "resume.pdf"
        source.write_bytes(b"approved resume")
        transport = CaptureTransport(binary="openclaw", profile="remote",
                                     uploads_dir=Path(tmp) / "uploads", approved_upload_hashes={})
        try:
            transport.execute("target-8", {"action": "upload", "target": "e15", "value": str(source)})
        except Exception as exc:
            assert "not bound to an approval SHA-256" in str(exc)
            return
        raise AssertionError("unbound artifact bytes were uploaded")
