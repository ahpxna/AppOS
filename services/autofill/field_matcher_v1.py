"""Deterministic field-to-profile matching; no browser and no LLM access."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from services.autofill.form_inspector_v1 import FormField
from services.common.immigration_semantics import legal_question_pause_reason


class FieldClass(StrEnum):
    STATIC = "STATIC"
    DERIVED = "DERIVED"
    SENSITIVE = "SENSITIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FieldMatch:
    field: FormField
    profile_key: str | None
    field_class: FieldClass
    confidence: float
    reason: str


_STATIC = (
    (r"\b(first|given)\s+name\b", "personal.first_name"),
    (r"\b(last|family|surname)\b", "personal.last_name"),
    (r"\b(e-?mail|email address)\b", "personal.email"),
    (r"\b(phone|mobile|telephone)\b", "personal.phone"),
    (r"linked-?in", "personal.linkedin"),
    (r"github", "personal.github"),
    (r"\b(portfolio|personal website)\b", "personal.portfolio"),
    (r"\b(university|college|school)\b", "education.university"),
    (r"\bmajor|field of study\b", "education.major"),
    (r"\bgraduation (date|year)\b", "education.graduation_date"),
)
_DERIVED = (
    (r"\byears?.{0,30}\bexperience\b", "derived.years_experience"),
    (r"\b(highest|education)\s+degree\b|^degree$", "education.degree"),
    (r"\bgpa\b", "education.gpa"),
)
_SENSITIVE = (r"\b(race|ethnicity|gender|disability|veteran|export control)\b",)


def match_field(field: FormField) -> FieldMatch:
    label = field.label.casefold()
    immigration_reason = legal_question_pause_reason(field.label)
    if immigration_reason:
        return FieldMatch(field, None, FieldClass.SENSITIVE, 1.0, immigration_reason)
    if any(re.search(pattern, label) for pattern in _SENSITIVE):
        return FieldMatch(field, None, FieldClass.SENSITIVE, 1.0, "Sensitive/legal field requires an exact user-confirmed answer.")
    for pattern, key in _STATIC:
        if re.search(pattern, label):
            return FieldMatch(field, key, FieldClass.STATIC, 0.99, "Exact deterministic label match.")
    for pattern, key in _DERIVED:
        if re.search(pattern, label):
            return FieldMatch(field, key, FieldClass.DERIVED, 0.90, "Derived value needs evidence and semantic review.")
    if field.document_hint in {"resume", "cover_letter"}:
        return FieldMatch(field, f"documents.{field.document_hint}", FieldClass.STATIC, 0.99, "Approved application document upload.")
    return FieldMatch(field, None, FieldClass.UNKNOWN, 0.0, "No deterministic profile mapping.")
