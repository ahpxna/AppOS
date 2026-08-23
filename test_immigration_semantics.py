from services.common.immigration_semantics import (
    ImmigrationQuestionClass,
    assess_jd_immigration_policy,
    classify_immigration_question,
    legal_question_pause_reason,
)
from services.discovery.immigration_intelligence import candidate_fit_status


class _Cursor:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return self.row


def test_sponsorship_now_or_future_is_not_collapsed_into_current_authorization():
    assert classify_immigration_question(
        "Will you now or in the future require visa sponsorship?"
    ) == ImmigrationQuestionClass.SPONSORSHIP_NOW_OR_FUTURE
    assert classify_immigration_question(
        "Are you legally authorized to work in the United States?"
    ) == ImmigrationQuestionClass.CURRENT_AUTHORIZATION


def test_ambiguous_sponsorship_question_fails_closed():
    assert classify_immigration_question("Will you require sponsorship?") == (
        ImmigrationQuestionClass.UNKNOWN_IMMIGRATION_QUESTION
    )
    assert "never autofilled" in (legal_question_pause_reason("Will you require sponsorship?") or "")


def test_jd_policy_is_explicit_and_never_uses_absence_as_compatibility():
    assert assess_jd_immigration_policy("We cannot provide visa sponsorship.").status == "BLOCKED"
    assert assess_jd_immigration_policy("STEM OPT candidates are welcome.").status == "POSSIBLE"
    unknown = assess_jd_immigration_policy("Build distributed services in Python.")
    assert unknown.status == "UNKNOWN"
    assert unknown.jd_policy_result == "unknown"


def test_explicit_no_sponsorship_only_blocks_after_candidate_confirmation():
    assert candidate_fit_status(_Cursor(None), "BLOCKED")[0] == "LOW"
    assert candidate_fit_status(_Cursor(("no", "yes", "2026-08-23")), "BLOCKED")[0] == "BLOCKED"
    assert candidate_fit_status(_Cursor(("no", "yes", "2026-08-23")), "UNKNOWN")[0] == "UNKNOWN"
