"""Pure policy for user-verified fixed resume fields and certifications."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class FieldDefinition:
    key: str
    group: str
    prompt: str
    required: bool = True
    show_on_resume_default: bool = True
    applicant_identity_key: str | None = None
    value_type: str = "text"
    reconfirm_days: int | None = None


FIELD_DEFINITIONS: tuple[FieldDefinition, ...] = (
    FieldDefinition("personal.full_name", "identity", "Full name exactly as it should appear on the resume", applicant_identity_key="full_name"),
    FieldDefinition("personal.email", "contact", "Resume email", applicant_identity_key="email", reconfirm_days=365),
    FieldDefinition("personal.phone", "contact", "Resume phone number", applicant_identity_key="phone", reconfirm_days=365),
    FieldDefinition("resume.location", "contact", "Resume location (city/country or preferred display)", required=False, reconfirm_days=180),
    FieldDefinition("resume.linkedin_url", "links", "LinkedIn URL", required=False, applicant_identity_key="linkedin_url", reconfirm_days=365),
    FieldDefinition("resume.github_url", "links", "GitHub profile URL", required=False, applicant_identity_key="github_url", reconfirm_days=365),
    FieldDefinition("education.university", "education", "University/institution name", applicant_identity_key="university_name"),
    FieldDefinition("education.degree", "education", "Degree name", applicant_identity_key="degree"),
    FieldDefinition("education.major", "education", "Major/subject", required=False, applicant_identity_key="major"),
    FieldDefinition("education.graduation_date", "education", "Graduation/completion date (YYYY-MM-DD or YYYY-MM)", applicant_identity_key="graduation_date", value_type="date"),
    FieldDefinition("education.gpa.show_on_resume", "education", "Show GPA on the resume? (yes/no)", value_type="bool", reconfirm_days=365),
    FieldDefinition("education.gpa.value", "education", "GPA value", required=False, applicant_identity_key="gpa", value_type="decimal"),
    FieldDefinition("education.gpa.scale", "education", "GPA scale (for example 4.0 or 10.0)", required=False, applicant_identity_key="gpa_scale", value_type="decimal"),
    FieldDefinition("education.gpa.status", "education", "Is that GPA current or final? (current/final)", required=False),
    FieldDefinition("certifications.reviewed", "certifications", "Have you reviewed the certifications to include? (yes/no)", value_type="bool", show_on_resume_default=False),
)
FIELD_BY_KEY = {field.key: field for field in FIELD_DEFINITIONS}
REQUIRED_FIELD_KEYS = tuple(field.key for field in FIELD_DEFINITIONS if field.required)


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError("Expected yes/no or true/false.")


def normalize_value(definition: FieldDefinition, value: Any) -> Any:
    if definition.value_type == "bool":
        return normalize_bool(value)
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if definition.value_type == "decimal":
        if not text:
            return ""
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"{definition.key} must be numeric.") from exc
        if number < 0:
            raise ValueError(f"{definition.key} cannot be negative.")
        return text
    if definition.value_type == "date" and text:
        if not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", text):
            raise ValueError(f"{definition.key} must be YYYY-MM or YYYY-MM-DD.")
    if definition.key == "education.gpa.status" and text.casefold() not in {"current", "final"}:
        raise ValueError("education.gpa.status must be current or final.")
    return text


def _verified_at_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date()


def readiness_from_records(fields: dict[str, dict[str, Any]], certifications: Iterable[dict[str, Any]], *, today: date | None = None) -> dict[str, Any]:
    """Return fixed-zone readiness without allowing model suggestions to become truth.

    ``reconfirm_days`` is intentionally conservative and applies only to contact/
    display fields that can genuinely age.  Permanent education facts do not
    decay merely because time passed.  A current GPA has a tighter 90-day
    policy when it is shown on the resume.
    """
    now_date = today or date.today()
    missing: list[str] = []
    conflicts: list[str] = []
    stale: list[str] = []
    verified_statuses = {"user_verified", "document_verified", "excluded"}

    for definition in FIELD_DEFINITIONS:
        row = fields.get(definition.key)
        if not row:
            if definition.required:
                missing.append(definition.key)
            continue
        status = str(row.get("verification_status") or "missing")
        if status == "conflict":
            conflicts.append(definition.key)
        visible = bool(row.get("show_on_resume", definition.show_on_resume_default))
        has_value = row.get("value") not in (None, "") and str(row.get("display_value") or "").strip() != ""
        if definition.required and status not in verified_statuses:
            missing.append(definition.key)
        elif not definition.required and visible and has_value and status not in verified_statuses:
            missing.append(definition.key)

        if status in {"user_verified", "document_verified"} and definition.reconfirm_days:
            verified_date = _verified_at_date(row.get("verified_at"))
            if verified_date is None or (now_date - verified_date).days > definition.reconfirm_days:
                stale.append(definition.key)

        expiry = row.get("expires_at")
        expiry_date = _verified_at_date(expiry)
        if status in {"user_verified", "document_verified"} and expiry_date and expiry_date < now_date:
            stale.append(definition.key)

    gpa_show_row = fields.get("education.gpa.show_on_resume") or {}
    gpa_show = bool(gpa_show_row.get("value")) if gpa_show_row.get("verification_status") in {"user_verified", "document_verified"} else False
    if gpa_show:
        for key in ("education.gpa.value", "education.gpa.scale", "education.gpa.status"):
            row = fields.get(key)
            if not row or row.get("verification_status") not in {"user_verified", "document_verified"} or not str(row.get("display_value") or "").strip():
                missing.append(key)
        status_row = fields.get("education.gpa.status") or {}
        if str(status_row.get("display_value") or "").casefold() == "current":
            for key in ("education.gpa.value", "education.gpa.scale", "education.gpa.status"):
                row = fields.get(key) or {}
                verified_date = _verified_at_date(row.get("verified_at"))
                if verified_date is None or (now_date - verified_date).days > 90:
                    stale.append(key)

    invalid_certifications: list[str] = []
    for cert in certifications:
        if not cert.get("show_on_resume"):
            continue
        status = cert.get("certification_status")
        verification = cert.get("verification_status")
        expiry = cert.get("expires_at")
        if isinstance(expiry, str) and expiry:
            try:
                expiry = date.fromisoformat(expiry)
            except ValueError:
                expiry = None
        if status != "earned" or verification not in {"user_verified", "document_verified"} or (expiry and expiry < now_date):
            invalid_certifications.append(str(cert.get("name") or "unknown certification"))

    missing = sorted(set(missing))
    stale = sorted(set(stale))
    return {
        "fixed_fields_ready": not missing and not conflicts and not stale and not invalid_certifications,
        "missing_fields": missing,
        "conflicting_fields": sorted(set(conflicts)),
        "stale_fields": stale,
        "invalid_visible_certifications": invalid_certifications,
        "gpa_show_on_resume": gpa_show,
    }

