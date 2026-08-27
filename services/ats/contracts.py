"""Dependency-free normalized contracts shared by ATS discovery and intake."""
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


class JDQuality(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    LISTING_STUB = "listing_stub"
    EMPTY = "empty"


MIN_COMPLETE_JD_CHARS = 200
_STUB_PHRASES = (
    "full description not fetched",
    "use --with-details",
    "see details",
    "view details",
    "see job details",
    "view job details",
    "click here to apply",
    "apply for this job",
    "apply now",
    "learn more and apply",
)
_REMOTE_NEGATIONS = (
    r"\bnot\s+(?:a\s+)?remote\b",
    r"\bno\s+remote\b",
    r"\bremote\s+(?:work|working)\s+(?:is\s+)?not\s+(?:available|offered|permitted)\b",
    r"\bdoes\s+not\s+(?:offer|allow)\s+remote\b",
    r"\bmust\s+(?:work|be)\s+(?:on\s*site|onsite|in\s+(?:the\s+)?office)\b",
)


def normalize_work_mode(value: str | None) -> WorkMode:
    text = re.sub(r"[\s_-]+", " ", str(value or "").casefold()).strip()
    if text in {"remote", "fully remote", "remote only", "work from home", "telecommute", "telecommuting"}:
        return WorkMode.REMOTE
    if text in {"hybrid", "hybrid remote", "flexible hybrid", "hybrid work"}:
        return WorkMode.HYBRID
    if text in {"on site", "onsite", "in office", "office", "office based", "in person"}:
        return WorkMode.ON_SITE
    return WorkMode.UNKNOWN


def infer_work_mode(*values: object) -> WorkMode:
    """Infer explicit work mode without letting negated ``remote`` win.

    Structured values such as schema.org ``TELECOMMUTE`` are accepted.  Hybrid
    is checked first because many postings contain both "remote" and "office"
    language while explicitly describing a hybrid schedule.
    """
    pieces = [str(v or "").strip() for v in values if str(v or "").strip()]
    for piece in pieces:
        exact = normalize_work_mode(piece)
        if exact != WorkMode.UNKNOWN:
            return exact

    normalized = re.sub(r"[\s_-]+", " ", " ".join(pieces).casefold()).strip()
    if not normalized:
        return WorkMode.UNKNOWN
    if re.search(r"\bhybrid\b", normalized):
        return WorkMode.HYBRID

    remote_negated = any(re.search(pattern, normalized) for pattern in _REMOTE_NEGATIONS)
    onsite_signal = bool(re.search(r"\b(on site|onsite|in office|office based|in person)\b", normalized))
    if remote_negated and onsite_signal:
        return WorkMode.ON_SITE
    if not remote_negated and re.search(r"\b(remote|work from home|fully remote|telecommut(?:e|ing))\b", normalized):
        return WorkMode.REMOTE
    if onsite_signal:
        return WorkMode.ON_SITE
    return WorkMode.UNKNOWN


def canonical_job_url(value: str | None) -> str:
    """Preserve identifying query parameters while removing presentation drift."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("job URL must be an absolute HTTP(S) URL")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                       parsed.path.rstrip("/") or "/", query, ""))


def assess_jd_quality(text: str | None) -> JDQuality:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return JDQuality.EMPTY
    lowered = value.casefold()
    # A short board teaser is not evidence for fit/document tailoring even if
    # it arrived inside a syntactically valid JobPosting object.
    if any(phrase in lowered for phrase in _STUB_PHRASES) and len(value) < 500:
        return JDQuality.LISTING_STUB
    if len(value) < MIN_COMPLETE_JD_CHARS:
        return JDQuality.PARTIAL
    # Reject pages that are effectively just title/company/location metadata.
    word_count = len(re.findall(r"\b[\w+#.-]+\b", value))
    if word_count < 35:
        return JDQuality.PARTIAL
    return JDQuality.COMPLETE


def jd_is_complete(text: str | None) -> bool:
    return assess_jd_quality(text) == JDQuality.COMPLETE


@dataclass(frozen=True)
class NormalizedJob:
    external_id: str
    title: str
    company: str
    canonical_url: str
    jd_text: str
    jd_quality: JDQuality
    location: str = ""
    work_mode: WorkMode = WorkMode.UNKNOWN
