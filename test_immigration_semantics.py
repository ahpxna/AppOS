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


def test_legal_question_classifier_is_exact_and_unknown_fails_closed():
    cases = {
        "Are you currently authorized to work in the United States?": ImmigrationQuestionClass.CURRENT_AUTHORIZATION,
        "Are you legally authorized to work in the US?": ImmigrationQuestionClass.CURRENT_AUTHORIZATION,
        "Will you now or in the future require sponsorship?": ImmigrationQuestionClass.SPONSORSHIP_NOW_OR_FUTURE,
        "Do you need visa sponsorship now or in the future?": ImmigrationQuestionClass.SPONSORSHIP_NOW_OR_FUTURE,
        "Will you require sponsorship to begin employment?": ImmigrationQuestionClass.SPONSORSHIP_TO_START,
        "Will you need visa sponsorship upon start?": ImmigrationQuestionClass.SPONSORSHIP_TO_START,
        "Are you currently on STEM OPT?": ImmigrationQuestionClass.CURRENT_STEM_OPT_STATUS,
        "Do you currently hold STEM OPT authorization?": ImmigrationQuestionClass.CURRENT_STEM_OPT_STATUS,
        "Will you require a STEM OPT extension?": ImmigrationQuestionClass.WILL_REQUIRE_STEM_EXTENSION,
        "Do you need STEM OPT in the future?": ImmigrationQuestionClass.WILL_REQUIRE_STEM_EXTENSION,
        "Will you require your employer to complete Form I-983?": ImmigrationQuestionClass.I983_REQUIREMENT,
        "Do you need an I 983 from the employer?": ImmigrationQuestionClass.I983_REQUIREMENT,
        "Is this employer enrolled in E-Verify?": ImmigrationQuestionClass.EMPLOYER_EVERIFY_REQUIREMENT,
        "Is the company E verify registered?": ImmigrationQuestionClass.EMPLOYER_EVERIFY_REQUIREMENT,
        "Are you a U.S. citizen?": ImmigrationQuestionClass.US_CITIZENSHIP,
        "Are you a US person?": ImmigrationQuestionClass.US_PERSON,
        "Do you have permanent work authorization?": ImmigrationQuestionClass.PERMANENT_WORK_AUTHORIZATION,
        "Please indicate your preferred employment category.": None,
        "Will you require visa sponsorship?": ImmigrationQuestionClass.UNKNOWN_IMMIGRATION_QUESTION,
    }
    for question, expected in cases.items():
        assert classify_immigration_question(question) is expected
