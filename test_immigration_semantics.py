from services.common.immigration_semantics import (
    ImmigrationQuestionClass,
    RestrictionType,
    assess_jd_immigration_policy,
    classify_immigration_question,
    legal_question_pause_reason,
)
from services.discovery.immigration_intelligence import synthesize_immigration_fit


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


def test_citizenship_is_not_conflated_with_sponsorship():
    policy = assess_jd_immigration_policy("U.S. citizenship required for this role.")
    assert policy.restriction_type is RestrictionType.US_CITIZENSHIP
    status, _ = synthesize_immigration_fit({"user_confirmed_at": "today", "us_citizen": "no"}, policy)
    assert status == "BLOCKED"


def test_high_requires_confirmed_profile_and_two_distinct_employer_signals():
    policy = assess_jd_immigration_policy("Build distributed services in Python.")
    status, _ = synthesize_immigration_fit(
        {"user_confirmed_at": "today", "current_status": "F1",
         "requires_sponsorship_to_start": "no", "requires_future_sponsorship": "yes",
         "stem_extension_eligible": True}, policy,
        everify_status="verified", h1b_history_status="positive",
    )
    assert status == "HIGH"
    status, _ = synthesize_immigration_fit({}, policy, everify_status="verified", h1b_history_status="positive")
    assert status == "POSSIBLE"
