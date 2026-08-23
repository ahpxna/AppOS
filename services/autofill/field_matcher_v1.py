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
    (r"^(your\s+)?(full\s+)?name$|\b(full|legal)\s+name\b", "personal.full_name"),
    (r"\b(first|given)\s+name\b", "personal.first_name"),
    (r"\b(last|family|surname)\b", "personal.last_name"),
    (r"\bmiddle\s+(name|initial)\b", "personal.middle_name"),
    (r"\bpreferred\s+name\b", "personal.preferred_name"),
    (r"\bpronouns\b", "personal.pronouns"),
    (r"\b(e-?mail|email address)\b", "personal.email"),
    (r"\bcountry\s+code\b", "personal.phone_country_code"),
    (r"\b(phone|mobile|telephone)\b", "personal.phone"),
    (r"linked-?in", "personal.linkedin"),
    (r"github", "personal.github"),
    (r"\b(portfolio|personal website)\b", "personal.portfolio"),
    (r"\b(address line 2|apt\.?|suite)\b", "address.line2"),
    (r"\b(address|street|address line 1)\b", "address.line1"),
    (r"\b(city|town)\b", "address.city"),
    (r"\b(state|province|region)\b", "address.state"),
    (r"\bpostal\s+code\s+extension\b", "address.postal_extension"),
    (r"\b(zip|postal)\b", "address.postal"),
    (r"\bcounty\b", "address.county"),
    (r"\bcountry\b", "address.country"),
    # A generic "School" label is ambiguous in repeated ATS education rows.
    # Only labels that explicitly say university/college are safe to fill.
    (r"\b(university|college)\b", "education.university"),
    (r"\bmajor|field of study\b", "education.major"),
    (r"\bgraduation\s*(date|year)?\b", "education.graduation_date"),
    (r"\bcurrent\s+(employer|company)\b", "employment.current_employer"),
    (r"\bcurrent\s+(job\s*)?title\b", "employment.current_title"),
    (r"\bdesired\s+(job\s*)?title\b", "employment.desired_title"),
    (r"how\s+did\s+you\s+hear\s+about", "preferences.referral_source"),
    (r"\b(x|twitter)\s*(profile|handle|url)?\b", "personal.twitter"),
    (r"\bother\s*(url|link|profile)\b", "personal.other_url"),
)
_DERIVED = (
    (r"\byears?.{0,30}\bexperience\b", "derived.years_experience"),
    (r"\b(highest|education)\s+degree\b|^degree$", "education.degree"),
    (r"\bgpa\b", "education.gpa"),
)
_SENSITIVE = (r"\b(race|ethnicity|gender|disability|veteran|export control)\b",)


def match_field(field: FormField) -> FieldMatch:
    label = field.label.casefold()
    if re.search(r"\b(high\s*school|secondary\s*(school|education)|ged)\b", label):
        return FieldMatch(field, None, FieldClass.UNKNOWN, 1.0,
                          "High-school/secondary education is not the approved university field.")
    if re.fullmatch(r"\s*(school|institution|education)\s*", label):
        return FieldMatch(field, None, FieldClass.UNKNOWN, 0.0,
                          "Ambiguous education-row label; select the correct education entry manually.")
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
