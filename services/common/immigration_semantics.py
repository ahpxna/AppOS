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
    PERMANENT_WORK_AUTHORIZATION = "PERMANENT_WORK_AUTHORIZATION"
    STEM_OPT_EMPLOYER_REQUIREMENT = "STEM_OPT_EMPLOYER_REQUIREMENT"
    UNKNOWN_IMMIGRATION_QUESTION = "UNKNOWN_IMMIGRATION_QUESTION"


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


def _normalise(text: str) -> str:
    return " ".join((text or "").casefold().split())


def is_immigration_question(question: str) -> bool:
    text = _normalise(question)
    return any(token in text for token in (
        "sponsor", "visa", "work authorization", "authorized to work",
        "citizen", "citizenship", "us person", "u.s. person", "opt",
        "e-verify", "e verify", "i-983", "permanent work authorization",
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
    if any(token in text for token in ("e-verify", "e verify", "i-983", "stem opt")):
        return ImmigrationQuestionClass.STEM_OPT_EMPLOYER_REQUIREMENT
    if any(token in text for token in ("citizen", "citizenship", "us person", "u.s. person")):
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


_BLOCKED_PATTERNS = (
    r"(?:no|unable to|cannot|can not|will not|does not)\s+(?:provide|offer|sponsor).{0,80}(?:visa|sponsorship)",
    r"(?:must not|cannot|may not).{0,80}(?:require|need).{0,80}(?:sponsorship|visa)",
    r"(?:authorized to work).{0,80}(?:without|no).{0,80}(?:sponsorship|visa)",
    r"(?:citizenship required|u\.?s\.? citizen(?:ship)? required|u\.?s\.? person required)",
)
_COMPATIBLE_PATTERNS = (
    r"(?:opt candidates?|f-?1 candidates?|stem opt).{0,80}(?:welcome|eligible|accepted|considered)",
    r"(?:welcome|eligible|accepted|consider).{0,80}(?:opt candidates?|f-?1 candidates?|stem opt)",
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
    """Classify explicit JD wording only; absence is UNKNOWN, never a pass."""
    blocked = _matching_phrases(jd_text or "", _BLOCKED_PATTERNS)
    if blocked:
        return ImmigrationAssessment(
            status="BLOCKED", jd_policy_result="incompatible", evidence=blocked,
            final_reason="The job description explicitly states an incompatible authorization or sponsorship policy.",
        )
    compatible = _matching_phrases(jd_text or "", _COMPATIBLE_PATTERNS)
    if compatible:
        return ImmigrationAssessment(
            status="POSSIBLE", jd_policy_result="compatible", evidence=compatible,
            final_reason="The JD explicitly mentions OPT/F-1 compatibility; future sponsorship still requires confirmation.",
        )
    return ImmigrationAssessment(
        status="UNKNOWN", jd_policy_result="unknown", evidence=(),
        final_reason="The JD contains no explicit immigration policy evidence; do not infer sponsorship from its absence.",
    )
