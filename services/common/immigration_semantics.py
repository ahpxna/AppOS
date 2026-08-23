"""Fail-closed helpers for immigration-related job questions and JD evidence.

This module classifies *meaning*, never the candidate's legal answer.  It is
intentionally conservative: an unrecognised immigration question pauses for a
human rather than falling through to keyword-based autofill.

It is not legal advice.  The profile data is candidate-confirmed input and a
candidate remains responsible for each employer-form attestation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class ImmigrationQuestionClass(StrEnum):
    CURRENT_AUTHORIZATION = "CURRENT_AUTHORIZATION"
    SPONSORSHIP_TO_START = "SPONSORSHIP_TO_START"
    SPONSORSHIP_NOW_OR_FUTURE = "SPONSORSHIP_NOW_OR_FUTURE"
    US_CITIZENSHIP = "US_CITIZENSHIP"
    US_PERSON = "US_PERSON"
    PERMANENT_WORK_AUTHORIZATION = "PERMANENT_WORK_AUTHORIZATION"
    CURRENT_STEM_OPT_STATUS = "CURRENT_STEM_OPT_STATUS"
    WILL_REQUIRE_STEM_EXTENSION = "WILL_REQUIRE_STEM_EXTENSION"
    I983_REQUIREMENT = "I983_REQUIREMENT"
    EMPLOYER_EVERIFY_REQUIREMENT = "EMPLOYER_EVERIFY_REQUIREMENT"
    UNKNOWN_IMMIGRATION_QUESTION = "UNKNOWN_IMMIGRATION_QUESTION"


# These do not share a meaning with the general F-1 eligibility profile and
# therefore need their own explicit candidate confirmation. Employer E-Verify
# is intentionally excluded because it is employer evidence, not applicant data.
EXACT_CANDIDATE_ADDITIONAL_CLASSES = frozenset((
    ImmigrationQuestionClass.CURRENT_STEM_OPT_STATUS,
    ImmigrationQuestionClass.WILL_REQUIRE_STEM_EXTENSION,
    ImmigrationQuestionClass.I983_REQUIREMENT,
))


class RestrictionType(StrEnum):
    """The legal meaning expressed by a job post, separate from its rank."""

    NO_SPONSORSHIP = "NO_SPONSORSHIP"
    PERMANENT_AUTHORIZATION = "PERMANENT_AUTHORIZATION"
    US_CITIZENSHIP = "US_CITIZENSHIP"
    US_PERSON = "US_PERSON"
    OPT_COMPATIBLE = "OPT_COMPATIBLE"
    STEM_OPT_COMPATIBLE = "STEM_OPT_COMPATIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ImmigrationAssessment:
    """Separate job-posting policy from employer evidence.

    `everify_status` and `h1b_history_status` are deliberately separate: an
    E-Verify match does not establish that a company will sponsor an H-1B.
    """

    status: str
    jd_policy_result: str
    evidence: tuple[str, ...]
    final_reason: str
    restriction_type: RestrictionType = RestrictionType.UNKNOWN


def _normalise(text: str) -> str:
    return " ".join((text or "").casefold().split())


def is_immigration_question(question: str) -> bool:
    text = _normalise(question)
    return any(token in text for token in (
        "sponsor", "visa", "work authorization", "authorized to work",
        "citizen", "citizenship", "us person", "u.s. person", "opt",
        "e-verify", "e verify", "i-983", "i 983", "permanent work authorization",
    ))


def classify_immigration_question(question: str) -> ImmigrationQuestionClass | None:
    """Classify an employer question without choosing Yes/No.

    Returns ``None`` for ordinary form fields.  A question that appears
    immigration-related but cannot be classified returns the explicit UNKNOWN
    class so a caller must pause it.
    """
    text = _normalise(question)
    if not is_immigration_question(text):
        return None
    # These terms request different attestations. Employer E-Verify is never
    # candidate data, so it remains paused unless employer evidence supplies it.
    if "e-verify" in text or "e verify" in text:
        return ImmigrationQuestionClass.EMPLOYER_EVERIFY_REQUIREMENT
    if "i-983" in text or "i 983" in text:
        return ImmigrationQuestionClass.I983_REQUIREMENT
    if "stem opt" in text:
        if any(token in text for token in ("currently", "current", "on stem opt")):
            return ImmigrationQuestionClass.CURRENT_STEM_OPT_STATUS
        if any(token in text for token in ("require", "need", "extension", "future")):
            return ImmigrationQuestionClass.WILL_REQUIRE_STEM_EXTENSION
        return ImmigrationQuestionClass.UNKNOWN_IMMIGRATION_QUESTION
    if "us person" in text or "u.s. person" in text:
        return ImmigrationQuestionClass.US_PERSON
    if any(token in text for token in ("citizen", "citizenship")):
        return ImmigrationQuestionClass.US_CITIZENSHIP
    if "permanent" in text or "indefinitely" in text:
        return ImmigrationQuestionClass.PERMANENT_WORK_AUTHORIZATION
    if "sponsor" in text or "visa" in text:
        if "future" in text or "now or in the future" in text:
            return ImmigrationQuestionClass.SPONSORSHIP_NOW_OR_FUTURE
        if any(token in text for token in ("to start", "upon start", "at the start", "begin employment")):
            return ImmigrationQuestionClass.SPONSORSHIP_TO_START
        return ImmigrationQuestionClass.UNKNOWN_IMMIGRATION_QUESTION
    if "authorized to work" in text or "work authorization" in text:
        return ImmigrationQuestionClass.CURRENT_AUTHORIZATION
    return ImmigrationQuestionClass.UNKNOWN_IMMIGRATION_QUESTION


def legal_question_pause_reason(question: str) -> str | None:
    """Return a human-review reason for any immigration-related prompt."""
    kind = classify_immigration_question(question)
    if kind is None:
        return None
    return (
        f"{kind.value}: immigration/work-authorization questions are never "
        "autofilled from a generic answer. Confirm this exact wording yourself."
    )


_NO_SPONSORSHIP_PATTERNS = (
    r"(?:no|unable to|cannot|can not|will not|does not)\s+(?:provide|offer|sponsor).{0,80}(?:visa|sponsorship)",
    r"(?:must not|cannot|may not).{0,80}(?:require|need).{0,80}(?:sponsorship|visa)",
    r"(?:authorized to work).{0,80}(?:without|no).{0,80}(?:sponsorship|visa)",
)
_PERMANENT_AUTHORIZATION_PATTERNS = (
    r"(?:permanent|unrestricted|indefinite).{0,100}(?:work authorization|authorization|right to work)",
    r"(?:must be|only).{0,80}(?:authorized|eligible).{0,80}(?:indefinitely|permanently)",
)
_US_CITIZENSHIP_PATTERNS = (
    r"(?:u\.?s\.? citizenship required|u\.?s\.? citizen(?:ship)? required|must be a u\.?s\.? citizen)",
)
_US_PERSON_PATTERNS = (
    r"(?:u\.?s\.? person required|must be a u\.?s\.? person)",
)
_STEM_OPT_PATTERNS = (
    r"(?:stem opt).{0,80}(?:welcome|eligible|accepted|considered|supported)",
    r"(?:welcome|eligible|accepted|consider).{0,80}(?:stem opt)",
)
_OPT_PATTERNS = (
    r"(?:opt candidates?|f-?1 candidates?).{0,80}(?:welcome|eligible|accepted|considered|supported)",
    r"(?:welcome|eligible|accepted|consider).{0,80}(?:opt candidates?|f-?1 candidates?)",
)


def _matching_phrases(text: str, patterns: Iterable[str]) -> tuple[str, ...]:
    matches: list[str] = []
    for pattern in patterns:
        for found in re.finditer(pattern, text, re.IGNORECASE):
            start, end = max(0, found.start() - 45), min(len(text), found.end() + 45)
            phrase = " ".join(text[start:end].split())
            if phrase and phrase not in matches:
                matches.append(phrase)
    return tuple(matches)


def assess_jd_immigration_policy(jd_text: str) -> ImmigrationAssessment:
    """Classify explicit JD wording only; absence is UNKNOWN, never a pass.

    Citizenship and US-person requirements are intentionally classified before
    sponsorship: they have different legal semantics and candidate evidence.
    """
    text = jd_text or ""
    rules = (
        (RestrictionType.US_CITIZENSHIP, _US_CITIZENSHIP_PATTERNS, "incompatible"),
        (RestrictionType.US_PERSON, _US_PERSON_PATTERNS, "incompatible"),
        (RestrictionType.PERMANENT_AUTHORIZATION, _PERMANENT_AUTHORIZATION_PATTERNS, "incompatible"),
        (RestrictionType.NO_SPONSORSHIP, _NO_SPONSORSHIP_PATTERNS, "incompatible"),
        (RestrictionType.STEM_OPT_COMPATIBLE, _STEM_OPT_PATTERNS, "compatible"),
        (RestrictionType.OPT_COMPATIBLE, _OPT_PATTERNS, "compatible"),
    )
    for restriction_type, patterns, result in rules:
        matches = _matching_phrases(text, patterns)
        if not matches:
            continue
        if result == "incompatible":
            return ImmigrationAssessment(
                status="BLOCKED", jd_policy_result=result, evidence=matches,
                final_reason=f"The JD explicitly states a {restriction_type.value} restriction.",
                restriction_type=restriction_type,
            )
        return ImmigrationAssessment(
            status="POSSIBLE", jd_policy_result=result, evidence=matches,
            final_reason=f"The JD explicitly mentions {restriction_type.value} compatibility; future sponsorship still requires confirmation.",
            restriction_type=restriction_type,
        )
    return ImmigrationAssessment(
        status="UNKNOWN", jd_policy_result="unknown", evidence=(),
        final_reason="The JD contains no explicit immigration policy evidence; do not infer sponsorship from its absence.",
        restriction_type=RestrictionType.UNKNOWN,
    )
