"""Deterministic discovery preferences; no LLM or hidden rejection logic."""
from __future__ import annotations

import re
from typing import Mapping


def normalized_terms(values: object) -> list[str]:
    return [str(item).casefold().strip() for item in (values or []) if str(item).strip()]


def preference_reason(*, company: str, title: str, location: str, work_mode: str,
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
    return None
