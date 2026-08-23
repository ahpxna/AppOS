"""Deterministic discovery preferences; no LLM or hidden rejection logic."""
from __future__ import annotations

import re
from typing import Mapping


def normalized_terms(values: object) -> list[str]:
    return [str(item).casefold().strip() for item in (values or []) if str(item).strip()]


def _employment_type(text: str) -> str | None:
    lowered = (text or "").casefold()
    for value, pattern in (("internship", r"\bintern(ship)?\b"), ("contract", r"\b(contract|contractor|1099)\b"),
                           ("full-time", r"\b(full[ -]?time)\b"), ("part-time", r"\bpart[ -]?time\b")):
        if re.search(pattern, lowered):
            return value
    return None


def _salary_ceiling(value: str) -> float | None:
    """Return a published upper bound only; unknown compensation is not rejected."""
    amounts = []
    for raw, suffix in re.findall(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([kK]?)", value or ""):
        amount = float(raw.replace(",", ""))
        amounts.append(amount * 1000 if suffix else amount)
    return max(amounts) if amounts else None


def preference_reason(*, company: str, title: str, location: str, work_mode: str,
                      jd_text: str = "", salary_range: str = "",
                      preferences: Mapping[str, object]) -> str | None:
    values = {"company": (company or "").casefold(), "title": (title or "").casefold(),
              "location": (location or "").casefold(), "work_mode": (work_mode or "").casefold()}
    for key, target in (("company_blacklist", "company"), ("title_blacklist", "title"),
                        ("location_blacklist", "location")):
        if any(term in values[target] for term in normalized_terms(preferences.get(key))):
            return f"{key}:{target}"
    allowed_modes = normalized_terms(preferences.get("allowed_work_modes"))
    if values["work_mode"] and allowed_modes and values["work_mode"] not in allowed_modes:
        return "work_mode_not_preferred"
    patterns = normalized_terms(preferences.get("location_allow_patterns"))
    if patterns and not any(re.search(pattern, values["location"], flags=re.I) for pattern in patterns):
        return "location_not_allowed"
    allowed_types = normalized_terms(preferences.get("allowed_employment_types"))
    employment_type = _employment_type(f"{title}\n{jd_text}")
    if employment_type and allowed_types and employment_type not in allowed_types:
        return "employment_type_not_preferred"
    floor = preferences.get("salary_floor")
    ceiling = _salary_ceiling(salary_range)
    if floor is not None and ceiling is not None and ceiling < float(floor):
        return "published_salary_below_floor"
    return None
