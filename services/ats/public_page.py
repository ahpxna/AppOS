"""Deterministic structured-web discovery fallback for ATS career pages.

This module never authenticates, clicks, submits, or executes arbitrary page
JavaScript. It extracts schema.org ``JobPosting`` records and follows only
links that stay on the employer board or move to a *known candidate-system
host* from the canonical ATS registry. JavaScript-only boards remain usable
through exact-URL/manual/browser intake without turning discovery into an
undocumented scraper.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

from services.ats.contracts import (
    JDQuality,
    WorkMode,
    assess_jd_quality,
    canonical_job_url,
    infer_work_mode,
)
from services.ats.http_client import DiscoveryHttpError, get_text
from services.ats.registry import detect_ats_platform


class PublicPageDiscoveryError(RuntimeError):
    """Typed structured-page failure that preserves HTTP retry semantics."""

    def __init__(self, message: str, *, kind: str = "structured_page",
                 transient: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.transient = bool(transient)

    @classmethod
    def from_http(cls, exc: DiscoveryHttpError) -> "PublicPageDiscoveryError":
        return cls(str(exc), kind=exc.kind, transient=exc.transient)


@dataclass(frozen=True)
class StructuredPage:
    final_url: str
    job_postings: tuple[dict[str, Any], ...]
    links: tuple[str, ...]


class _StructuredPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._in_ld_json = False
        self._ld_parts: list[str] = []
        self.ld_json: list[Any] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_map = {str(k).casefold(): str(v or "") for k, v in attrs}
        if tag.casefold() == "a" and attrs_map.get("href"):
            self.links.append(attrs_map["href"])
        if tag.casefold() == "script" and "ld+json" in attrs_map.get("type", "").casefold():
            self._in_ld_json = True
            self._ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._in_ld_json:
            return
        raw = "".join(self._ld_parts).strip()
        self._in_ld_json = False
        self._ld_parts = []
        if not raw:
            return
        try:
            self.ld_json.append(json.loads(raw))
        except json.JSONDecodeError:
            # Malformed JSON-LD is not repaired heuristically.
            return


def _walk_job_postings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _walk_job_postings(item)
        return
    if not isinstance(value, dict):
        return
    raw_type = value.get("@type")
    types = {str(v).casefold() for v in raw_type} if isinstance(raw_type, list) else {str(raw_type or "").casefold()}
    if "jobposting" in types:
        yield value
    for key in ("@graph", "itemListElement", "mainEntity", "hasPart", "item"):
        child = value.get(key)
        if child is not None:
            yield from _walk_job_postings(child)


def _walk_structured_urls(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            yield from _walk_structured_urls(item)
        return
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "/")):
            yield value
        return
    if not isinstance(value, dict):
        return
    for key in ("url", "@id"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            yield raw.strip()
    for key in ("@graph", "itemListElement", "mainEntity", "hasPart", "item"):
        child = value.get(key)
        if child is not None:
            yield from _walk_structured_urls(child)


def parse_structured_page(html_text: str, *, base_url: str) -> StructuredPage:
    parser = _StructuredPageParser()
    try:
        parser.feed(html_text)
    except Exception as exc:
        raise PublicPageDiscoveryError(f"invalid HTML from {base_url}: {exc}") from exc
    postings: list[dict[str, Any]] = []
    for payload in parser.ld_json:
        postings.extend(_walk_job_postings(payload))
    links: list[str] = []
    seen: set[str] = set()
    structured_links: list[str] = []
    for payload in parser.ld_json:
        structured_links.extend(_walk_structured_urls(payload))
    for raw in [*structured_links, *parser.links]:
        url = urljoin(base_url, raw)
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            links.append(url)
    return StructuredPage(base_url, tuple(postings), tuple(links))


def _strip_html(value: Any) -> str:
    raw = str(value or "")
    if "<" not in raw:
        return html.unescape(raw).strip()
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return " ".join(html.unescape(raw).split())


def _organization_name(job: dict[str, Any], fallback: str) -> str:
    org = job.get("hiringOrganization")
    if isinstance(org, dict):
        return str(org.get("name") or fallback).strip()
    return fallback.strip()


def _location_text(value: Any) -> str:
    locations = value if isinstance(value, list) else [value]
    out: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            if loc:
                out.append(str(loc))
            continue
        address = loc.get("address") if isinstance(loc.get("address"), dict) else loc
        parts = [address.get("addressLocality"), address.get("addressRegion"),
                 address.get("postalCode"), address.get("addressCountry")]
        text = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        if text:
            out.append(text)
        elif loc.get("name"):
            out.append(str(loc["name"]).strip())
    return " | ".join(dict.fromkeys(x for x in out if x))


def _identifier(job: dict[str, Any], *, url: str, title: str) -> str:
    value = job.get("identifier")
    if isinstance(value, dict):
        value = value.get("value") or value.get("name")
    if value:
        return str(value).strip()
    for key in ("jobId", "requisitionId", "positionId"):
        if job.get(key):
            return str(job[key]).strip()
    if url:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return hashlib.sha256(title.encode("utf-8")).hexdigest()[:32]


def normalize_jobposting(job: dict[str, Any], *, page_url: str, company_hint: str) -> dict[str, Any] | None:
    title = _strip_html(job.get("title") or job.get("name"))
    if not title:
        return None
    raw_url = str(job.get("url") or page_url).strip()
    try:
        url = canonical_job_url(raw_url)
    except ValueError:
        return None
    description = _strip_html(job.get("description"))
    if not description:
        return None
    company = _organization_name(job, company_hint)
    location = _location_text(job.get("jobLocation"))
    department = _strip_html(job.get("occupationalCategory"))
    # Schema.org jobLocationType is more authoritative than prose; passing it
    # first lets TELECOMMUTE win without a stray "not remote" sentence doing so.
    work_mode = infer_work_mode(str(job.get("jobLocationType") or ""), location, description)
    header = f"{title}\nCompany: {company}"
    if location:
        header += f"\nLocation: {location}"
    if department:
        header += f"\nDepartment: {department}"
    jd_text = f"{header}\n\n{description}".strip()
    quality = assess_jd_quality(jd_text)
    return {
        "external_id": _identifier(job, url=url, title=title),
        "title": title,
        "location": location,
        "department": department,
        "remote": work_mode == WorkMode.REMOTE,
        "work_mode": work_mode.value,
        "url": url,
        "jd_text": jd_text,
        "jd_quality": quality.value,
    }


def is_candidate_job_link(url: str, *, board_url: str, platform: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    board_host = (urlsplit(board_url).hostname or "").casefold()
    target_host = parsed.hostname.casefold()
    target_platform = detect_ats_platform(url)
    if target_host != board_host:
        # Company-owned careers sites very commonly hand off to Workday/iCIMS/
        # Oracle/etc.  Permit that exact cross-host link only when the canonical
        # registry recognizes the target as a candidate system. Unknown external
        # domains remain refused.
        if platform == "custom":
            if target_platform == "custom":
                return False
        elif target_platform != platform:
            return False
    path = (parsed.path or "/").casefold()
    query = (parsed.query or "").casefold()
    signals = (
        "/job/", "/jobs/", "/jobdetails", "/job-detail", "/position/", "/positions/",
        "/opportunity/", "/opportunities/", "/careersection/", "/requisition/", "/vacancy/",
        "/vacancies/", "/posting/", "/postings/", "/apply/",
    )
    return any(signal in path for signal in signals) or any(
        key in query for key in ("jobid=", "job_id=", "requisitionid=", "requisition_id=")
    )


def fetch_public_job_board(*, career_url: str, platform: str, company_hint: str,
                           user_agent: str, timeout_seconds: int = 30,
                           max_details: int = 100,
                           fetcher: Callable[..., tuple[str, str]] = get_text) -> list[dict[str, Any]]:
    """Fetch only *complete* schema.org jobs from a public board/detail chain."""
    try:
        canonical_board = canonical_job_url(career_url)
    except ValueError as exc:
        raise PublicPageDiscoveryError(str(exc), kind="invalid_url") from exc
    try:
        body, final_url = fetcher(url=canonical_board, user_agent=user_agent, timeout_seconds=timeout_seconds)
    except DiscoveryHttpError as exc:
        raise PublicPageDiscoveryError.from_http(exc) from exc
    page = parse_structured_page(body, base_url=final_url)

    jobs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    incomplete_urls: list[str] = []

    def add(raw_job: dict[str, Any], source_url: str) -> None:
        normalized = normalize_jobposting(raw_job, page_url=source_url, company_hint=company_hint)
        if not normalized:
            return
        if normalized["jd_quality"] != JDQuality.COMPLETE.value:
            if normalized.get("url"):
                incomplete_urls.append(str(normalized["url"]))
            return
        identity = (normalized["external_id"], normalized["url"])
        if identity not in seen:
            seen.add(identity)
            jobs.append(normalized)

    for raw_job in page.job_postings:
        add(raw_job, final_url)

    # A board can publish listing-stub JobPosting objects *and* exact detail
    # URLs. Do not return the stubs; follow those URLs and require complete JDs.
    candidate_links = [*incomplete_urls, *page.links]
    detail_urls: list[str] = []
    detail_seen: set[str] = set()
    for link in candidate_links:
        if link in detail_seen:
            continue
        if is_candidate_job_link(link, board_url=final_url, platform=platform):
            detail_seen.add(link)
            detail_urls.append(link)

    last_transient: DiscoveryHttpError | None = None
    for detail_url in detail_urls[:max(0, min(int(max_details), 200))]:
        try:
            detail_body, detail_final_url = fetcher(
                url=detail_url, user_agent=user_agent, timeout_seconds=timeout_seconds
            )
        except DiscoveryHttpError as exc:
            if exc.transient:
                last_transient = exc
            continue
        detail = parse_structured_page(detail_body, base_url=detail_final_url)
        for raw_job in detail.job_postings:
            add(raw_job, detail_final_url)

    if jobs:
        return jobs
    if last_transient is not None:
        raise PublicPageDiscoveryError.from_http(last_transient) from last_transient
    raise PublicPageDiscoveryError(
        f"{platform} career page exposed no complete deterministic schema.org JobPosting data; "
        "use an exact job URL from another intake source or a tenant-specific adapter.",
        kind="incomplete_or_missing_jobposting",
        transient=False,
    )
