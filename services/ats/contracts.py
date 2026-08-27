"""Small, dependency-free normalized ATS vocabulary."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class WorkMode(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ON_SITE = "on_site"
    UNKNOWN = "unknown"


def normalize_work_mode(value: str | None) -> WorkMode:
    text = re.sub(r"[\s_-]+", " ", str(value or "").casefold()).strip()
    if text in {"remote", "fully remote", "remote only", "work from home"}:
        return WorkMode.REMOTE
    if text in {"hybrid", "hybrid remote", "flexible hybrid"}:
        return WorkMode.HYBRID
    if text in {"on site", "onsite", "in office", "office"}:
        return WorkMode.ON_SITE
    return WorkMode.UNKNOWN


def canonical_job_url(value: str | None) -> str:
    """Preserve a job's identifying query while removing presentation drift.

    ATS requisition IDs often live in query parameters, so unlike generic web
    URL cleaners this never drops query keys.  Fragments and ordering are not
    part of a posting's durable identity.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("job URL must be an absolute HTTP(S) URL")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                       parsed.path.rstrip("/") or "/", query, ""))


@dataclass(frozen=True)
class NormalizedJob:
    external_id: str
    title: str
    company: str
    canonical_url: str
    jd_text: str
    jd_quality: str
    location: str = ""
    work_mode: WorkMode = WorkMode.UNKNOWN


def jd_is_complete(text: str | None) -> bool:
    value = str(text or "").strip()
    placeholder = "(full description not fetched -- use --with-details)"
    return bool(value) and placeholder not in value.casefold()
