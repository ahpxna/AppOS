"""Deterministic discovery preferences; no LLM or hidden rejection logic."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Mapping

import regex as safe_regex

MAX_LOCATION_REGEX_LENGTH = 160
LOCATION_REGEX_TIMEOUT_SECONDS = 0.02
_UNSAFE_REGEX = re.compile(r"(?:\([^)]*[+*][^)]*\)[+*])|(?:\.\*[+*])|(?:\{\d{3,}(?:,\d*)?\})")


def validate_location_pattern(value: object) -> str:
    pattern = str(value or "").strip()
    if not pattern or len(pattern) > MAX_LOCATION_REGEX_LENGTH:
        raise ValueError("location regex must be 1..160 characters")
    if _UNSAFE_REGEX.search(pattern):
        raise ValueError("location regex contains unsupported pathological repetition")
    try:
        safe_regex.compile(pattern, flags=safe_regex.I)
    except safe_regex.error as exc:
        raise ValueError(f"invalid location regex: {exc}") from exc
    return pattern


@lru_cache(maxsize=256)
def _compiled_location_pattern(pattern: str):
    return safe_regex.compile(validate_location_pattern(pattern), flags=safe_regex.I)


def _safe_location_match(pattern: str, value: str) -> bool:
    try:
        return bool(_compiled_location_pattern(pattern).search(
            value, timeout=LOCATION_REGEX_TIMEOUT_SECONDS
        ))
    except (ValueError, safe_regex.error, TimeoutError):
        # Old/corrupt/pathological preference rows must fail closed for that
        # pattern, not abort or monopolize an otherwise valid discovery run.
        return False


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
    if patterns and not any(_safe_location_match(pattern, values["location"]) for pattern in patterns):
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
