"""
L0/L1 -- GENERALIZED ATS DISCOVERY

All discovery paths land complete postings in ``applications`` at the same
canonical ``intake`` boundary. Seven ATS families retain native public JSON
adapters (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable and
Breezy). Other registered ATS families use deterministic schema.org JobPosting
discovery on official career pages; company pages may follow links to a known
candidate-system host from the canonical ATS registry. Unknown/proprietary
portals use the same ``custom`` fallback.

Structured discovery never authenticates, submits, guesses undocumented JSON
endpoints, or treats a listing stub as a full JD. JavaScript-only boards remain
reachable through the exact-URL/read-only browser or manual intake paths, so a
discovery limitation does not create an unsupported application pipeline.

LinkedIn discovery remains a separate feature with its existing safety and
checkpoint behavior; this module does not change that surface.

Usage:
  python services/discovery/ats_discovery_v1.py test --platform greenhouse --slug SLUG
  python services/discovery/ats_discovery_v1.py add --company "Acme" --platform greenhouse --slug acme --apply
  python services/discovery/ats_discovery_v1.py list
  python services/discovery/ats_discovery_v1.py poll --apply
  python services/discovery/ats_discovery_v1.py poll --company-id <uuid> --apply
"""

from __future__ import annotations

# JOBOS_DIRECT_FILE_BOOTSTRAP: keep direct `python path/to/file.py` usable
# while package imports resolve exactly as they do under `python -m ...`.
import sys as _jobos_sys
from pathlib import Path as _JobOSPath
_JOBOS_ROOT = _JobOSPath(__file__).resolve().parents[2]
if str(_JOBOS_ROOT) not in _jobos_sys.path:
    _jobos_sys.path.insert(0, str(_JOBOS_ROOT))

import argparse
import hashlib
import html
import json
import os
import re
import sys
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from services.discovery.captcha_detector import analyze_captcha_risk
import logging

import psycopg
from psycopg.types.json import Jsonb
from services.discovery.immigration_intelligence import record_jd_immigration_assessment
from services.common.config import database_dsn
from services.common.value_coercion import coerce_bool
from services.ats.contracts import canonical_job_url, jd_is_complete, normalize_work_mode
from services.ats.http_client import DiscoveryHttpError, get_json
from services.ats.public_page import PublicPageDiscoveryError, fetch_public_job_board
from services.ats.browser_discovery import BrowserDiscoveryError, discover_public_jobs_with_browser
from services.ats.registry import (
    DiscoveryStrategy, detect_ats_platform, discovery_platform_keys, get_definition, normalize_ats_key,
)
from services.intake.posting_identity import build_posting_identity
from services.intake.source_observation import find_and_observe_existing, observe_existing_posting

DISCOVERY_VERSION = "ats_discovery_v1_2026_07_31"
USER_AGENT = "jobos-ats-discovery/1 (personal job search tool, contact via GitHub repo)"
REQUEST_TIMEOUT = 30
STALE_CLOSE_DAYS = max(1, int(os.getenv("JOBOS_STALE_CLOSE_DAYS", "14")))
DETAIL_REQUEST_BUDGET = max(1, int(os.getenv("JOBOS_ATS_DETAIL_REQUEST_BUDGET", "100")))

PLATFORMS = discovery_platform_keys()


class DiscoveryError(Exception):
    """Typed discovery failure; retryability must survive adapter wrapping."""

    def __init__(self, message: str, *, kind: str = "adapter", transient: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.transient = bool(transient)

    @classmethod
    def from_http(cls, exc: DiscoveryHttpError, *, prefix: str = "") -> "DiscoveryError":
        message = f"{prefix}{exc}" if prefix else str(exc)
        return cls(message, kind=exc.kind, transient=exc.transient)


# ---------------------------------------------------------------- http

from services.discovery.captcha_detector import analyze_captcha_risk

def http_get_json(url: str) -> Any:
    try:
        return get_json(url=url, user_agent=USER_AGENT, timeout_seconds=REQUEST_TIMEOUT)
    except DiscoveryHttpError as e:
        # The HTML/CAPTCHA distinction is still useful to the existing
        # adapters; typed retryability remains available to callers through
        # the exception cause without changing their public CLI contract.
        if e.kind == "invalid_json":
            is_blocked, reason = analyze_captcha_risk(e.body_preview, url)
            if is_blocked:
                raise DiscoveryError(
                    f"Bị chặn bởi Anti-Bot/CAPTCHA: {reason}", kind="anti_bot", transient=False
                ) from e
            raise DiscoveryError(
                f"Non-JSON response from {url}: {e.body_preview[:200]!r}",
                kind=e.kind, transient=e.transient,
            ) from e
        raise DiscoveryError.from_http(e) from e


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text so job descriptions don't need a new dependency
    (bs4/lxml) just for this one module. Good enough for JD prose; not a
    general-purpose renderer."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("br", "p", "li", "div", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", " ".join(self._parts)).strip()



def html_to_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    if "<" not in raw:
        return html.unescape(raw).strip()
    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", raw)).strip()
    return html.unescape(parser.text())


def build_jd_text(*, title: str, company: str, location: str,
                  department: str, body: str) -> str:
    header = f"{title}\nCompany: {company}"
    if location:
        header += f"\nLocation: {location}"
    if department:
        header += f"\nDepartment: {department}"
    return f"{header}\n\n{body}".strip()


# ---------------------------------------------------------------- adapters
#
# Each adapter returns a list of normalized dicts:
#   {external_id, title, location, department, remote, url, jd_text}


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _object_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def fetch_greenhouse(slug: str) -> List[Dict[str, Any]]:
    data = _mapping(http_get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    ))
    out = []
    for j in _object_list(data.get("jobs")):
        location = _text(_mapping(j.get("location")).get("name"))
        departments = _object_list(j.get("departments"))
        department = _text(departments[0].get("name")) if departments else ""
        body = html_to_text(_text(j.get("content")))
        out.append({
            "external_id": _text(j.get("id")),
            "title": _text(j.get("title")),
            "location": location,
            "department": department,
            "remote": "remote" in location.lower(),
            "url": _text(j.get("absolute_url")),
            "jd_text": build_jd_text(
                title=_text(j.get("title")), company=slug, location=location,
                department=department, body=body,
            ),
        })
    return out


def fetch_lever(slug: str) -> List[Dict[str, Any]]:
    data = _object_list(http_get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
    out = []
    for j in data:
        categories = _mapping(j.get("categories"))
        location = _text(categories.get("location"))
        department = _text(categories.get("team") or categories.get("department"))
        body = _text(j.get("descriptionPlain")) or html_to_text(_text(j.get("description")))
        lists = _object_list(j.get("lists"))
        extra = "\n\n".join(
            f"{_text(blk.get('text'))}\n{html_to_text(_text(blk.get('content')))}"
            for blk in lists
        )
        out.append({
            "external_id": _text(j.get("id")),
            "title": _text(j.get("text")),
            "location": location,
            "department": department,
            "remote": "remote" in location.lower(),
            "url": _text(j.get("hostedUrl")),
            "jd_text": build_jd_text(
                title=_text(j.get("text")), company=slug, location=location,
                department=department, body=(body + "\n\n" + extra).strip(),
            ),
        })
    return out


def fetch_ashby(slug: str) -> List[Dict[str, Any]]:
    data = _mapping(http_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
    out = []
    for j in _object_list(data.get("jobs")):
        location = _text(j.get("location") or j.get("locationName"))
        department = _text(j.get("department") or j.get("team"))
        body = html_to_text(_text(j.get("descriptionHtml"))) or _text(j.get("descriptionPlain"))
        url = _text(j.get("jobUrl") or j.get("applyUrl"))
        out.append({
            "external_id": str(j.get("id", "")),
            "title": _text(j.get("title")),
            "location": location,
            "department": department,
            "remote": coerce_bool(j.get("isRemote")) or "remote" in location.lower(),
            "url": url,
            "jd_text": build_jd_text(
                title=_text(j.get("title")), company=slug, location=location,
                department=department, body=body,
            ),
        })
    return out


def fetch_smartrecruiters(slug: str, *, with_details: bool = False) -> List[Dict[str, Any]]:
    data = _mapping(http_get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"))
    out = []
    for position, j in enumerate(_object_list(data.get("content"))):
        loc = _mapping(j.get("location"))
        location = ", ".join(
            p for p in (_text(loc.get("city")), _text(loc.get("region")), _text(loc.get("country"))) if p
        )
        department = (_text(_mapping(j.get("department")).get("label"))
                     or _text(_mapping(j.get("function")).get("label")))
        job_id = _text(j.get("id"))
        url = _text(j.get("applyUrl") or j.get("postingUrl"))
        body = ""
        if with_details and job_id and position < DETAIL_REQUEST_BUDGET:
            try:
                detail = _mapping(http_get_json(
                    f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
                ))
                sections = _mapping(_mapping(detail.get("jobAd")).get("sections"))
                body = "\n\n".join(
                    html_to_text(_text(_mapping(sections.get(k)).get("text")))
                    for k in ("jobDescription", "qualifications", "additionalInformation")
                    if _mapping(sections.get(k))
                )
            except DiscoveryError:
                body = ""
        out.append({
            "external_id": str(job_id),
            "title": _text(j.get("name")),
            "location": location,
            "department": department,
            "remote": coerce_bool(loc.get("remote")) or "remote" in location.lower(),
            "url": url,
            "jd_text": build_jd_text(
                title=_text(j.get("name")), company=slug, location=location,
                department=department,
                body=body or "(full description not fetched -- use --with-details)",
            ),
        })
    return out


def fetch_recruitee(slug: str) -> List[Dict[str, Any]]:
    data = _mapping(http_get_json(f"https://{slug}.recruitee.com/api/offers/"))
    out = []
    for j in _object_list(data.get("offers")):
        location = _text(j.get("location") or j.get("city"))
        department = _text(j.get("department"))
        body = html_to_text(_text(j.get("description"))) + "\n\n" + html_to_text(_text(j.get("requirements")))
        out.append({
            "external_id": _text(j.get("id")),
            "title": _text(j.get("title")),
            "location": location,
            "department": department,
            "remote": coerce_bool(j.get("remote")) or "remote" in location.lower(),
            "url": _text(j.get("careers_url")),
            "jd_text": build_jd_text(
                title=_text(j.get("title")), company=slug, location=location,
                department=department, body=body.strip(),
            ),
        })
    return out


def fetch_workable(slug: str, *, with_details: bool = False) -> List[Dict[str, Any]]:
    data = _mapping(http_get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}"))
    out = []
    for position, j in enumerate(_object_list(data.get("jobs"))):
        loc = _mapping(j.get("location"))
        location = ", ".join(
            p for p in (_text(loc.get("city")), _text(loc.get("region")), _text(loc.get("country"))) if p
        )
        department = _text(j.get("department"))
        shortcode = _text(j.get("shortcode"))
        body = ""
        if with_details and shortcode and position < DETAIL_REQUEST_BUDGET:
            try:
                detail = _mapping(http_get_json(
                    f"https://apply.workable.com/api/v1/widget/accounts/{slug}/jobs/{shortcode}"
                ))
                body = html_to_text(_text(detail.get("description")))
            except DiscoveryError:
                body = ""
        out.append({
            "external_id": shortcode or _text(j.get("title")),
            "title": _text(j.get("title")),
            "location": location,
            "department": department,
            "remote": coerce_bool(loc.get("remote")) or "remote" in location.lower(),
            "url": _text(j.get("url")),
            "jd_text": build_jd_text(
                title=_text(j.get("title")), company=slug, location=location,
                department=department,
                body=body or "(full description not fetched -- use --with-details)",
            ),
        })
    return out


def fetch_breezy(slug: str) -> List[Dict[str, Any]]:
    data = _object_list(http_get_json(f"https://{slug}.breezy.hr/json"))
    out = []
    for j in data:
        loc = _mapping(j.get("location"))
        location = _text(loc.get("name"))
        department = _text(j.get("department") or j.get("type"))
        body = html_to_text(_text(j.get("description")))
        out.append({
            "external_id": _text(j.get("_id") or j.get("id")),
            "title": _text(j.get("name")),
            "location": location,
            "department": department,
            "remote": "remote" in location.lower(),
            "url": _text(j.get("url")),
            "jd_text": build_jd_text(
                title=_text(j.get("name")), company=slug, location=location,
                department=department, body=body,
            ),
        })
    return out


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workable": fetch_workable,
    "breezy": fetch_breezy,
}


def fetch_jobs(platform: str, slug: str | None, *, with_details: bool = False,
               source_url: str | None = None, company: str | None = None) -> List[Dict[str, Any]]:
    platform = normalize_ats_key(platform)
    slug = str(slug or "").strip()
    adapter = ADAPTERS.get(platform)
    if adapter is not None:
        if not slug:
            raise DiscoveryError(
                f"{get_definition(platform).display_name} native discovery requires --slug/tenant-key."
            )
        if platform in ("smartrecruiters", "workable"):
            return adapter(slug, with_details=with_details)
        return adapter(slug)

    definition = get_definition(platform)
    if definition.discovery_strategy == DiscoveryStrategy.EXTERNAL_SOURCE:
        raise DiscoveryError(
            f"{definition.display_name} is modeled as an external-source platform; "
            "use its dedicated intake path instead of ATS polling."
        )
    career_url = str(source_url or "").strip()
    if not career_url:
        raise DiscoveryError(
            f"{definition.display_name} has no stable native public list adapter. "
            "Configure --source-url with the employer's official careers page so JobOS can "
            "use deterministic schema.org JobPosting discovery."
        )
    try:
        return fetch_public_job_board(
            career_url=career_url, platform=platform, company_hint=str(company or slug or platform),
            user_agent=USER_AGENT, timeout_seconds=REQUEST_TIMEOUT,
            max_details=DETAIL_REQUEST_BUDGET,
        )
    except PublicPageDiscoveryError as exc:
        # Only a deterministic "no complete structured posting" result may
        # fall through to the read-only rendered-browser adapter. HTTP 429/5xx,
        # invalid URLs and other typed failures preserve their original retry
        # semantics and are never hidden behind a second network path.
        if (exc.kind == "incomplete_or_missing_jobposting"
                and os.getenv("JOBOS_ATS_BROWSER_DISCOVERY_ENABLED", "1").strip().casefold()
                    not in {"0", "false", "no", "off"}):
            try:
                return discover_public_jobs_with_browser(
                    career_url=career_url, platform=platform,
                    company_hint=str(company or slug or platform),
                    max_details=min(DETAIL_REQUEST_BUDGET, 50),
                )
            except BrowserDiscoveryError as browser_exc:
                # Preserve the strongest retry signal. Browser availability is
                # not itself a transient source failure; a browser timeout is.
                if browser_exc.transient:
                    raise DiscoveryError(
                        str(browser_exc), kind=browser_exc.kind, transient=True
                    ) from browser_exc
                raise DiscoveryError(
                    f"{exc}; read-only browser fallback: {browser_exc}",
                    kind=exc.kind, transient=exc.transient,
                ) from browser_exc
        raise DiscoveryError(str(exc), kind=exc.kind, transient=exc.transient) from exc


# ---------------------------------------------------------------- db

def intake_job(cur, *, jd_text: str, company: str, job_title: str, job_url: str,
               location: str, work_mode: str, ats_type: str,
               ats_company_id: str, ats_external_id: str) -> Optional[str]:
    """Create or observe one source-stable discovered posting.

    Once an application leaves ``intake`` its evidence snapshot is immutable;
    later board edits are recorded by ``observe_existing_posting`` rather than
    silently rewriting the JD used by fit/docs/approval.
    """
    jd_text = jd_text.strip()
    identity = build_posting_identity(
        company=company, job_title=job_title, jd_text=jd_text, job_url=job_url, ats_hint=ats_type
    )
    ats_type = identity.ats_type if identity.ats_type != "custom" else normalize_ats_key(ats_type)
    existing, _observation = find_and_observe_existing(
        cur, identity=identity, ats_company_id=ats_company_id, source_job_id=ats_external_id,
        source_name=ats_type, company=company, job_title=job_title, jd_text=jd_text,
        location=location, work_mode=work_mode,
        metadata={"ats_company_id": ats_company_id, "ats_external_id": ats_external_id},
    )
    if existing:
        return None
    cur.execute(
        """
        INSERT INTO applications
          (source, company, job_title, job_url, jd_text, jd_hash,
           current_step, status, intake_channel, ats_type, location, work_mode,
           ats_company_id, ats_external_id, source_job_id,
           first_seen_at, last_seen_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'intake', 'active', 'ats_discovery',
                %s, %s, %s, %s, %s, %s, now(), now(), now(), now())
        RETURNING id::text;
        """,
        (ats_type, company, job_title, identity.canonical_url, jd_text, identity.jd_hash,
         ats_type, location, normalize_work_mode(work_mode).value,
         ats_company_id, ats_external_id, ats_external_id),
    )
    app_id = cur.fetchone()[0]
    # Record the first source snapshot through the same append-only boundary so
    # subsequent polls have one provenance model rather than a special case.
    observe_existing_posting(
        cur, application_id=app_id, source_name=ats_type, source_job_id=ats_external_id,
        company=company, job_title=job_title, job_url=identity.canonical_url,
        jd_text=jd_text, jd_hash=identity.jd_hash, location=location, work_mode=work_mode,
        metadata={"ats_company_id": ats_company_id, "ats_external_id": ats_external_id, "initial": True},
    )
    immigration = record_jd_immigration_assessment(cur, app_id, jd_text)
    cur.execute(
        """
        INSERT INTO pipeline_events
          (application_id, from_step, to_step, actor, reason, detail_json)
        VALUES (%s, NULL, 'intake', 'ats_discovery', 'Discovered via ATS source.', %s);
        """,
        (app_id, Jsonb({"ats_type": ats_type, "ats_external_id": ats_external_id,
                        "immigration_assessment": immigration})),
    )
    return app_id


def _validated_company_locator(platform: str, slug: str | None, source_url: str | None) -> tuple[str, str | None, str | None]:
    platform = normalize_ats_key(platform)
    slug = str(slug or "").strip() or None
    source_url = str(source_url or "").strip() or None
    definition = get_definition(platform)
    if platform in ADAPTERS:
        if not slug:
            raise DiscoveryError(f"{definition.display_name} native discovery requires --slug/tenant-key.")
    elif definition.discovery_strategy != DiscoveryStrategy.EXTERNAL_SOURCE:
        if not source_url:
            raise DiscoveryError(
                f"{definition.display_name} structured/browser discovery requires --source-url; "
                "do not invent a vendor slug."
            )
        try:
            source_url = canonical_job_url(source_url)
        except ValueError as exc:
            raise DiscoveryError(str(exc), kind="invalid_url") from exc
    return platform, slug, source_url


def cmd_add(conn, args) -> int:
    try:
        platform, slug, source_url = _validated_company_locator(
            args.platform, args.slug, args.source_url
        )
    except DiscoveryError as exc:
        print(f"  ERROR: {exc}")
        return 1
    with conn.cursor() as cur:
        if slug:
            cur.execute(
                "SELECT id::text FROM ats_companies WHERE ats_platform=%s AND slug=%s FOR UPDATE;",
                (platform, slug),
            )
        else:
            cur.execute(
                "SELECT id::text FROM ats_companies WHERE ats_platform=%s AND source_url=%s FOR UPDATE;",
                (platform, source_url),
            )
        existing = cur.fetchone()
        if existing:
            company_id = str(existing[0])
            cur.execute(
                """UPDATE ats_companies
                      SET company_name=%s, slug=%s, source_url=%s, notes=%s, updated_at=now()
                    WHERE id=%s;""",
                (args.company, slug, source_url, args.notes, company_id),
            )
        else:
            cur.execute(
                """INSERT INTO ats_companies (company_name, ats_platform, slug, source_url, notes)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id::text;""",
                (args.company, platform, slug, source_url, args.notes),
            )
            company_id = str(cur.fetchone()[0])
        if not args.apply:
            conn.rollback()
            locator = slug or source_url or "?"
            print(f"DRY RUN. Would add/update {args.company} ({platform}:{locator}).")
            return 0
        conn.commit()
        locator = slug or source_url or "?"
        print(f"  saved: {company_id}  {args.company}  {platform}:{locator}")
    return 0


def cmd_list(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, company_name, ats_platform, slug, source_url, enabled,
                   last_polled_at, last_success_at, last_job_count, consecutive_failures
            FROM ats_companies ORDER BY company_name;
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("\nNo companies configured yet. Add one with:")
            print("  Native adapter:     python services/discovery/ats_discovery_v1.py add "
                  "--company NAME --platform greenhouse --slug TENANT --apply")
            print("  Structured adapter: python services/discovery/ats_discovery_v1.py add "
                  "--company NAME --platform workday --source-url URL --apply")
            return 0
        print(f"\n{'COMPANY':<28} {'PLATFORM':<16} {'LOCATOR':<34} {'EN':<4} "
              f"{'LAST POLL':<20} {'JOBS':<6} FAILS")
        for cid, name, plat, slug, source_url, en, polled, success, jobs, fails in rows:
            locator = str(slug or source_url or "-")
            print(f"{(name or '?')[:28]:<28} {plat:<16} {locator[:34]:<34} "
                  f"{'y' if en else 'n':<4} {str(polled)[:19] if polled else '-':<20} "
                  f"{jobs if jobs is not None else '-':<6} {fails}")
    return 0


def cmd_test(conn, args) -> int:
    print(f"  fetching {args.platform}:{args.slug or args.source_url or '?'} (no DB writes) ...")
    try:
        jobs = fetch_jobs(args.platform, args.slug, with_details=args.with_details, source_url=args.source_url, company=args.company)
    except DiscoveryError as e:
        print(f"\n  ERROR: {e}")
        return 1
    print(f"\n  {len(jobs)} posting(s) found")
    for j in jobs[:args.limit]:
        print(f"\n  [{j['external_id']}] {j['title']}")
        print(f"    location:   {j['location'] or '(none)'}")
        print(f"    department: {j['department'] or '(none)'}")
        print(f"    url:        {j['url']}")
        print(f"    jd chars:   {len(j['jd_text'])}")
    if len(jobs) > args.limit:
        print(f"\n  ... and {len(jobs) - args.limit} more (--limit to show more)")
    return 0


def poll_company(conn, cid, name, platform, slug, source_url, *, apply: bool, with_details: bool):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ats_discovery_runs (ats_company_id, started_at) "
            "VALUES (%s, now()) RETURNING id::text;",
            (cid,),
        )
        run_id = cur.fetchone()[0]
    conn.commit()

    seen = new = dup = 0
    ok = True
    err = None
    error_kind: str | None = None
    transient_failure = False
    try:
        jobs = fetch_jobs(platform, slug, with_details=with_details, source_url=source_url, company=name)
        seen = len(jobs)
        for j in jobs:
            if not j["title"] or not jd_is_complete(j.get("jd_text")):
                continue
            with conn.cursor() as cur:
                app_id = None
                if apply:
                    detected = detect_ats_platform(j.get("url"))
                    job_ats_type = detected if detected != "custom" else platform
                    app_id = intake_job(
                        cur, jd_text=j["jd_text"], company=name, job_title=j["title"],
                        job_url=j["url"], location=j["location"],
                        work_mode=str(j.get("work_mode") or ("remote" if j.get("remote") else "unknown")),
                        ats_type=job_ats_type, ats_company_id=cid,
                        ats_external_id=j["external_id"],
                    )
            if app_id:
                new += 1
                print(f"    NEW  [{platform}] {name} -- {j['title']}")
            else:
                dup += 1
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except DiscoveryError as e:
        ok = False
        err = str(e)
        error_kind = e.kind
        transient_failure = e.transient
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ats_discovery_runs
            SET finished_at = now(), ok = %s, jobs_seen = %s,
                jobs_new = %s, jobs_duplicate = %s, error = %s
            WHERE id = %s;
            """,
            (ok, seen, new, dup, err, run_id),
        )
        if apply:
            cur.execute(
                """
                UPDATE ats_companies
                SET last_polled_at = now(),
                    last_success_at = CASE WHEN %s THEN now() ELSE last_success_at END,
                    last_job_count = %s,
                    consecutive_failures = CASE WHEN %s THEN 0 ELSE consecutive_failures + 1 END,
                    last_error_kind = CASE WHEN %s THEN NULL ELSE %s END,
                    next_retry_at = CASE
                        WHEN %s THEN NULL
                        WHEN %s THEN now() + make_interval(
                            secs => LEAST(3600, 15 * power(2, LEAST(consecutive_failures, 8))::int)
                        )
                        ELSE now() + interval '24 hours'
                    END,
                    updated_at = now()
                WHERE id = %s;
                """,
                (ok, seen, ok, ok, error_kind, ok, transient_failure, cid),
            )
            if ok:
                cur.execute(
                    """UPDATE applications
                       SET stale_at = now(), status = 'stale', updated_at = now()
                     WHERE ats_company_id = %s AND source = %s
                       AND intake_channel = 'ats_discovery' AND current_step = 'intake'
                       AND status = 'active'
                       AND last_seen_at < (SELECT started_at FROM ats_discovery_runs WHERE id = %s);""",
                    (cid, platform, run_id),
                )
                # A posting that remains absent after the configurable grace
                # window is closed for discovery/ranking, while retained for
                # audit and demand-analysis history.
                cur.execute(
                    """UPDATE applications
                           SET closed_at = now(), status = 'closed', updated_at = now()
                         WHERE ats_company_id = %s AND source = %s
                           AND intake_channel = 'ats_discovery' AND current_step = 'intake'
                           AND status = 'stale' AND stale_at < now() - make_interval(days => %s);""",
                    (cid, platform, STALE_CLOSE_DAYS),
                )
    conn.commit()
    return ok, seen, new, dup, err


def cmd_poll(conn, args) -> int:
    with conn.cursor() as cur:
        if args.company_id:
            cur.execute(
                "SELECT id::text, company_name, ats_platform, slug, source_url "
                "FROM ats_companies WHERE id = %s;",
                (args.company_id,),
            )
        else:
            cur.execute(
                """
                SELECT id::text, company_name, ats_platform, slug, source_url
                FROM ats_companies
                WHERE enabled = true
                  AND (next_retry_at IS NULL OR next_retry_at <= now())
                ORDER BY last_polled_at NULLS FIRST;
                """,
            )
        rows = cur.fetchall()

    if not rows:
        print("Nothing to poll. Add companies first (see `add` command), "
              "or they may all be disabled / past the failure threshold.")
        return 0

    total_new = 0
    for cid, name, platform, slug, source_url in rows:
        print(f"\n  polling {name} ({platform}:{slug or source_url or '?'}) ...")
        ok, seen, new, dup, err = poll_company(
            conn, cid, name, platform, slug, source_url,
            apply=args.apply, with_details=args.with_details,
        )
        if ok:
            print(f"    seen={seen} new={new} duplicate={dup}")
            total_new += new
        else:
            print(f"    FAILED: {err}")

    print(f"\n{'DRY RUN. ' if not args.apply else ''}Total new postings: {total_new}")
    if not args.apply:
        print("Nothing was written. Re-run with --apply to intake them.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS ATS API discovery")
    sub = p.add_subparsers(dest="command", required=True)

    pt = sub.add_parser("test", help="Fetch one company, print results, write nothing.")
    pt.add_argument("--platform", required=True, choices=PLATFORMS)
    pt.add_argument("--slug", help="Native ATS tenant/company key; required only for native adapters.")
    pt.add_argument("--source-url", help="Official careers URL for structured-web fallback.")
    pt.add_argument("--company", help="Company name for structured-web normalization.")
    test_detail_mode = pt.add_mutually_exclusive_group()
    test_detail_mode.add_argument("--with-details", dest="with_details", action="store_true", default=True,
                                  help="Fetch full JDs (the default; retained for script compatibility).")
    test_detail_mode.add_argument("--summary-only", dest="with_details", action="store_false",
                                  help="Inspect listing summaries only; never suitable for --apply intake.")
    pt.add_argument("--limit", type=int, default=10)

    pa = sub.add_parser("add", help="Add or update a company to poll.")
    pa.add_argument("--company", required=True)
    pa.add_argument("--platform", required=True, choices=PLATFORMS)
    pa.add_argument("--slug", help="Native ATS tenant/company key; omit for structured/browser adapters.")
    pa.add_argument("--source-url", help="Official careers URL; required for non-native ATS polling.")
    pa.add_argument("--notes")
    pa.add_argument("--apply", action="store_true")

    sub.add_parser("list", help="List configured companies.")

    pp = sub.add_parser("poll", help="Fetch postings for configured companies and intake new ones.")
    pp.add_argument("--company-id")
    poll_detail_mode = pp.add_mutually_exclusive_group()
    poll_detail_mode.add_argument("--with-details", dest="with_details", action="store_true", default=True,
                                  help="Fetch full JDs (the default; retained for script compatibility).")
    poll_detail_mode.add_argument("--summary-only", dest="with_details", action="store_false",
                                  help="Inspect listing summaries only; incomplete JDs are refused at intake.")
    pp.add_argument("--max-consecutive-failures", type=int, default=5,
                    help="Deprecated compatibility option; retries now use bounded cooldowns instead of permanent automatic suppression.")
    pp.add_argument("--apply", action="store_true")

    args = p.parse_args()
    print(f"===== ATS DISCOVERY ({DISCOVERY_VERSION}) =====")

    if args.command == "test":
        return cmd_test(None, args)

    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        return {
            "add": cmd_add, "list": cmd_list, "poll": cmd_poll,
        }[args.command](conn, args)


if __name__ == "__main__":
    sys.exit(main())
