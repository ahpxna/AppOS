"""Canonical identity for company research cache rows and consumers."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from services.ats.registry import detect_ats_platform

_SHARED_HOSTS = ("linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com",
                 "dice.com", "wellfound.com")


def normalize_company_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def normalize_company_domain(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    host = (urlsplit(raw if "://" in raw else f"https://{raw}").hostname or "").strip(".").casefold()
    return host[4:] if host.startswith("www.") else host


def employer_domain_from_job_url(value: Any) -> str:
    raw = str(value or "").strip()
    host = normalize_company_domain(raw)
    if not host or detect_ats_platform(raw) != "custom":
        return ""
    if any(host == suffix or host.endswith("." + suffix) for suffix in _SHARED_HOSTS):
        return ""
    return host


def company_identity_key(company: Any, domain: Any = None) -> str:
    normalized_domain = normalize_company_domain(domain)
    if normalized_domain:
        return f"domain:{normalized_domain}"
    name = normalize_company_name(company)
    if not name:
        raise ValueError("company research identity requires a company name or domain")
    return f"name:{name}"


def research_cache_lookup_predicate(alias_table: str = "company_research_identity_aliases") -> str:
    """SQL predicate for the canonical cache row or a durable identity alias.

    A posting on LinkedIn/Greenhouse commonly begins with name-only identity,
    then research discovers the employer domain. Consumers must resolve both
    to the same row rather than repeatedly paying for research.
    """
    return (
        "(crc.identity_key = %s OR crc.id = "
        f"(SELECT research_cache_id FROM {alias_table} WHERE identity_key=%s))"
    )
