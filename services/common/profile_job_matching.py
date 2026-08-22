"""Deterministic, explainable job matching helpers.

The matching layer deliberately ranks only using terms from *approved* profile
capabilities plus user-provided search terms.  It is a discovery aid, not an
automated hiring decision.
"""
from __future__ import annotations

import re
from typing import Iterable


def normalize_term(value: str) -> str:
    """Normalize a human search term without splitting useful phrases."""
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def unique_terms(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        term = normalize_term(value)
        if len(term) < 2 or len(term) > 100 or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


def rank_job(*, title: str, jd_text: str, profile_terms: Iterable[str],
             user_keywords: Iterable[str] = ()) -> dict:
    """Score one job and return the exact matching terms.

    A title match weighs 4 and a JD-body match weighs 1.  The score is not a
    fit verdict; it makes the order inspectable before the existing L5 fit
    analysis runs.
    """
    title_l = (title or "").lower()
    jd_l = (jd_text or "").lower()
    profile = unique_terms(profile_terms)
    requested = unique_terms(user_keywords)
    all_terms = unique_terms([*profile, *requested])
    matched_profile: list[str] = []
    matched_keywords: list[str] = []
    score = 0

    for term in all_terms:
        in_title = term in title_l
        in_jd = term in jd_l
        if not (in_title or in_jd):
            continue
        score += (4 if in_title else 0) + (1 if in_jd else 0)
        (matched_keywords if term in requested else matched_profile).append(term)

    return {
        "discovery_score": score,
        "matched_profile_terms": matched_profile,
        "matched_user_keywords": matched_keywords,
    }
