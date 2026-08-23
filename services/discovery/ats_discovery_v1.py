"""
L0/L1 -- ATS API DISCOVERY

Polls public, unauthenticated read endpoints across multiple ATS platforms
and lands new postings in `applications` at current_step='intake', exactly
like a hand-entered JD. Everything downstream (no_llm_filter_rules, the L5
fit gate, cost gate, research, doc generation) applies unmodified -- this
module's only job is filling the front door.

This is an additional intake option, not a mandate to delete every other
source of jobs. If you already have another intake path you trust, keep it;
this module just gives you a safe public-API path that is easier to automate
than scraping arbitrary pages.

Why not LinkedIn: automating LinkedIn violates its ToS and is detected via
Chrome-over-CDP well enough that the realistic outcome is losing the
account, not job leads. See the architecture review. These read APIs are
public by design (job boards *want* postings crawled) and need no
credential:

  Greenhouse       https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  Lever            https://api.lever.co/v0/postings/{slug}?mode=json
  Ashby            https://api.ashbyhq.com/posting-api/job-board/{slug}
  SmartRecruiters  https://api.smartrecruiters.com/v1/companies/{slug}/postings
  Recruitee        https://{slug}.recruitee.com/api/offers/
  Workable         https://apply.workable.com/api/v1/widget/accounts/{slug}
  Breezy           https://{slug}.breezy.hr/json

Platforms deliberately NOT implemented: Workday, iCIMS, Taleo,
SuccessFactors. None of these expose a slug-based public JSON list endpoint
the way the ones above do -- each Workday/iCIMS tenant's search API is
usually POST-based, per-tenant, and undocumented enough that a generic
adapter would be guesswork dressed up as support. Add those companies by
hand with orchestrator_v1.py's `intake` command instead of trusting a
fragile scraper for them.

This module was written without live network access to any of the above
(the environment it was built in only reaches an allowlisted set of
domains) -- the endpoint shapes are implemented from each platform's public
documentation, not verified against a live response. Run `test` against a
real slug on your own machine before trusting `poll --apply`:

Usage:
  python services/discovery/ats_discovery_v1.py test --platform greenhouse --slug SLUG
  python services/discovery/ats_discovery_v1.py add --company "Acme" --platform greenhouse --slug acme --apply
  python services/discovery/ats_discovery_v1.py list
  python services/discovery/ats_discovery_v1.py poll --apply
  python services/discovery/ats_discovery_v1.py poll --company-id <uuid> --apply
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from services.discovery.captcha_detector import analyze_captcha_risk
import logging

import psycopg
from psycopg.types.json import Jsonb
from services.discovery.immigration_intelligence import record_jd_immigration_assessment
from services.common.config import database_dsn

DSN = database_dsn()

DISCOVERY_VERSION = "ats_discovery_v1_2026_07_31"
USER_AGENT = "jobos-ats-discovery/1 (personal job search tool, contact via GitHub repo)"
REQUEST_TIMEOUT = 30
STALE_CLOSE_DAYS = max(1, int(os.getenv("JOBOS_STALE_CLOSE_DAYS", "14")))

PLATFORMS = (
    "greenhouse", "lever", "ashby", "smartrecruiters", "recruitee",
    "workable", "breezy",
)


class DiscoveryError(Exception):
    pass


# ---------------------------------------------------------------- http

from services.discovery.captcha_detector import analyze_captcha_risk

def http_get_json(url: str) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise DiscoveryError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise DiscoveryError(f"Request failed for {url}: {e}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        # [THÊM MỚI] Check CAPTCHA nếu API trả về HTML thay vì JSON
        is_blocked, reason = analyze_captcha_risk(body, url)
        if is_blocked:
            raise DiscoveryError(f"Bị chặn bởi Anti-Bot/CAPTCHA: {reason}")
        raise DiscoveryError(f"Non-JSON response from {url}: {body[:200]!r}") from e


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

def fetch_greenhouse(slug: str) -> List[Dict[str, Any]]:
    data = http_get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    )
    out = []
    for j in data.get("jobs", []) or []:
        location = ((j.get("location") or {}).get("name") or "").strip()
        departments = j.get("departments") or []
        department = departments[0].get("name", "") if departments else ""
        body = html_to_text(j.get("content", ""))
        out.append({
            "external_id": str(j.get("id")),
            "title": j.get("title", "").strip(),
            "location": location,
            "department": department,
            "remote": "remote" in location.lower(),
            "url": j.get("absolute_url", ""),
            "jd_text": build_jd_text(
                title=j.get("title", ""), company=slug, location=location,
                department=department, body=body,
            ),
        })
    return out


def fetch_lever(slug: str) -> List[Dict[str, Any]]:
    data = http_get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data or []:
        categories = j.get("categories") or {}
        location = (categories.get("location") or "").strip()
        department = (categories.get("team") or categories.get("department") or "").strip()
        body = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
        lists = j.get("lists") or []
        extra = "\n\n".join(
            f"{blk.get('text', '')}\n{html_to_text(blk.get('content', ''))}"
            for blk in lists if isinstance(blk, dict)
        )
        out.append({
            "external_id": str(j.get("id")),
            "title": j.get("text", "").strip(),
            "location": location,
            "department": department,
            "remote": "remote" in location.lower(),
            "url": j.get("hostedUrl", ""),
            "jd_text": build_jd_text(
                title=j.get("text", ""), company=slug, location=location,
                department=department, body=(body + "\n\n" + extra).strip(),
            ),
        })
    return out


def fetch_ashby(slug: str) -> List[Dict[str, Any]]:
    data = http_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []) or []:
        location = (j.get("location") or j.get("locationName") or "").strip()
        department = (j.get("department") or j.get("team") or "").strip()
        body = html_to_text(j.get("descriptionHtml", "")) or j.get("descriptionPlain", "")
        url = j.get("jobUrl") or j.get("applyUrl", "")
        out.append({
            "external_id": str(j.get("id", "")),
            "title": (j.get("title") or "").strip(),
            "location": location,
            "department": department,
            "remote": bool(j.get("isRemote")) or "remote" in location.lower(),
            "url": url,
            "jd_text": build_jd_text(
                title=j.get("title", ""), company=slug, location=location,
                department=department, body=body,
            ),
        })
    return out


def fetch_smartrecruiters(slug: str, *, with_details: bool = False) -> List[Dict[str, Any]]:
    data = http_get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    out = []
    for j in data.get("content", []) or []:
        loc = j.get("location") or {}
        location = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
        department = ((j.get("department") or {}).get("label")
                     or (j.get("function") or {}).get("label") or "")
        job_id = j.get("id", "")
        url = j.get("applyUrl") or j.get("postingUrl") or ""
        body = ""
        if with_details and job_id:
            try:
                detail = http_get_json(
                    f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
                )
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                body = "\n\n".join(
                    html_to_text((sections.get(k) or {}).get("text", ""))
                    for k in ("jobDescription", "qualifications", "additionalInformation")
                    if sections.get(k)
                )
            except DiscoveryError:
                body = ""
        out.append({
            "external_id": str(job_id),
            "title": j.get("name", "").strip(),
            "location": location,
            "department": department,
            "remote": bool(loc.get("remote")) or "remote" in location.lower(),
            "url": url,
            "jd_text": build_jd_text(
                title=j.get("name", ""), company=slug, location=location,
                department=department,
                body=body or "(full description not fetched -- use --with-details)",
            ),
        })
    return out


def fetch_recruitee(slug: str) -> List[Dict[str, Any]]:
    data = http_get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []) or []:
        location = j.get("location", "") or j.get("city", "") or ""
        department = j.get("department", "") or ""
        body = html_to_text(j.get("description", "")) + "\n\n" + html_to_text(j.get("requirements", ""))
        out.append({
            "external_id": str(j.get("id")),
            "title": j.get("title", "").strip(),
            "location": location,
            "department": department,
            "remote": bool(j.get("remote")) or "remote" in location.lower(),
            "url": j.get("careers_url", ""),
            "jd_text": build_jd_text(
                title=j.get("title", ""), company=slug, location=location,
                department=department, body=body.strip(),
            ),
        })
    return out


def fetch_workable(slug: str, *, with_details: bool = False) -> List[Dict[str, Any]]:
    data = http_get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    out = []
    for j in data.get("jobs", []) or []:
        loc = j.get("location") or {}
        location = ", ".join(
            p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
        )
        department = j.get("department", "") or ""
        shortcode = j.get("shortcode", "")
        body = ""
        if with_details and shortcode:
            try:
                detail = http_get_json(
                    f"https://apply.workable.com/api/v1/widget/accounts/{slug}/jobs/{shortcode}"
                )
                body = html_to_text(detail.get("description", ""))
            except DiscoveryError:
                body = ""
        out.append({
            "external_id": shortcode or j.get("title", ""),
            "title": j.get("title", "").strip(),
            "location": location,
            "department": department,
            "remote": bool(loc.get("remote")) or "remote" in location.lower(),
            "url": j.get("url", ""),
            "jd_text": build_jd_text(
                title=j.get("title", ""), company=slug, location=location,
                department=department,
                body=body or "(full description not fetched -- use --with-details)",
            ),
        })
    return out


def fetch_breezy(slug: str) -> List[Dict[str, Any]]:
    data = http_get_json(f"https://{slug}.breezy.hr/json")
    out = []
    for j in data or []:
        loc = j.get("location") or {}
        location = loc.get("name", "") if isinstance(loc, dict) else str(loc or "")
        department = j.get("department", "") or j.get("type", "") or ""
        body = html_to_text(j.get("description", ""))
        out.append({
            "external_id": str(j.get("_id") or j.get("id", "")),
            "title": j.get("name", "").strip(),
            "location": location,
            "department": department,
            "remote": "remote" in location.lower(),
            "url": j.get("url", ""),
            "jd_text": build_jd_text(
                title=j.get("name", ""), company=slug, location=location,
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


def fetch_jobs(platform: str, slug: str, *, with_details: bool = False) -> List[Dict[str, Any]]:
    adapter = ADAPTERS.get(platform)
    if not adapter:
        raise DiscoveryError(f"Unsupported platform: {platform!r}. Supported: {', '.join(PLATFORMS)}")
    if platform in ("smartrecruiters", "workable"):
        return adapter(slug, with_details=with_details)
    return adapter(slug)


# ---------------------------------------------------------------- db

def intake_job(cur, *, jd_text: str, company: str, job_title: str, job_url: str,
               location: str, work_mode: str, ats_type: str,
               ats_company_id: str, ats_external_id: str) -> Optional[str]:
    """Create or refresh one source-stable discovered posting."""
    jd_text = jd_text.strip()
    jd_hash = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()

    cur.execute(
        """SELECT id::text, jd_hash FROM applications
           WHERE source = %s AND ats_company_id = %s AND source_job_id = %s;""",
        (ats_type, ats_company_id, ats_external_id),
    )
    existing = cur.fetchone()
    if existing:
        changed = existing[1] != jd_hash
        cur.execute(
            """UPDATE applications
               SET job_title = %s, job_url = %s, jd_text = %s, jd_hash = %s,
                   location = %s, work_mode = %s, last_seen_at = now(),
                   last_content_change_at = CASE WHEN %s THEN now() ELSE last_content_change_at END,
                   stale_at = NULL, closed_at = NULL, updated_at = now()
             WHERE id = %s;""",
            (job_title, job_url, jd_text, jd_hash, location,
             ("remote" if work_mode else None), changed, existing[0]),
        )
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
        (ats_type, company, job_title, job_url, jd_text, jd_hash,
         ats_type, location, ("remote" if work_mode else None),
         ats_company_id, ats_external_id, ats_external_id),
    )
    app_id = cur.fetchone()[0]
    immigration = record_jd_immigration_assessment(cur, app_id, jd_text)
    cur.execute(
        """
        INSERT INTO pipeline_events
          (application_id, from_step, to_step, actor, reason, detail_json)
        VALUES (%s, NULL, 'intake', 'ats_discovery', 'Discovered via ATS API.', %s);
        """,
        (app_id, Jsonb({"ats_type": ats_type, "ats_external_id": ats_external_id,
                        "immigration_assessment": immigration})),
    )
    return app_id


def cmd_add(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ats_companies (company_name, ats_platform, slug, notes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (ats_platform, slug) DO UPDATE
            SET company_name = EXCLUDED.company_name, notes = EXCLUDED.notes,
                updated_at = now()
            RETURNING id::text;
            """,
            (args.company, args.platform, args.slug, args.notes),
        )
        company_id = cur.fetchone()[0]
        if not args.apply:
            conn.rollback()
            print(f"DRY RUN. Would add/update {args.company} ({args.platform}:{args.slug}).")
            return 0
        conn.commit()
        print(f"  saved: {company_id}  {args.company}  {args.platform}:{args.slug}")
    return 0


def cmd_list(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, company_name, ats_platform, slug, enabled,
                   last_polled_at, last_success_at, last_job_count, consecutive_failures
            FROM ats_companies ORDER BY company_name;
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("\nNo companies configured yet. Add one with:")
            print("  python services/discovery/ats_discovery_v1.py add "
                  "--company NAME --platform PLATFORM --slug SLUG --apply")
            return 0
        print(f"\n{'COMPANY':<28} {'PLATFORM':<16} {'SLUG':<20} {'EN':<4} "
              f"{'LAST POLL':<20} {'JOBS':<6} FAILS")
        for cid, name, plat, slug, en, polled, success, jobs, fails in rows:
            print(f"{(name or '?')[:28]:<28} {plat:<16} {slug:<20} "
                  f"{'y' if en else 'n':<4} {str(polled)[:19] if polled else '-':<20} "
                  f"{jobs if jobs is not None else '-':<6} {fails}")
    return 0


def cmd_test(conn, args) -> int:
    print(f"  fetching {args.platform}:{args.slug} (no DB writes) ...")
    try:
        jobs = fetch_jobs(args.platform, args.slug, with_details=args.with_details)
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


def poll_company(conn, cid, name, platform, slug, *, apply: bool, with_details: bool):
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
    try:
        jobs = fetch_jobs(platform, slug, with_details=with_details)
        seen = len(jobs)
        for j in jobs:
            if not j["title"] or not j["jd_text"].strip():
                continue
            with conn.cursor() as cur:
                app_id = None
                if apply:
                    app_id = intake_job(
                        cur, jd_text=j["jd_text"], company=name, job_title=j["title"],
                        job_url=j["url"], location=j["location"],
                        work_mode="remote" if j["remote"] else "",
                        ats_type=platform, ats_company_id=cid,
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
                    updated_at = now()
                WHERE id = %s;
                """,
                (ok, seen, ok, cid),
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
                "SELECT id::text, company_name, ats_platform, slug "
                "FROM ats_companies WHERE id = %s;",
                (args.company_id,),
            )
        else:
            cur.execute(
                """
                SELECT id::text, company_name, ats_platform, slug
                FROM ats_companies
                WHERE enabled = true AND consecutive_failures < %s
                ORDER BY last_polled_at NULLS FIRST;
                """,
                (args.max_consecutive_failures,),
            )
        rows = cur.fetchall()

    if not rows:
        print("Nothing to poll. Add companies first (see `add` command), "
              "or they may all be disabled / past the failure threshold.")
        return 0

    total_new = 0
    for cid, name, platform, slug in rows:
        print(f"\n  polling {name} ({platform}:{slug}) ...")
        ok, seen, new, dup, err = poll_company(
            conn, cid, name, platform, slug,
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
    pt.add_argument("--slug", required=True)
    pt.add_argument("--with-details", action="store_true",
                    help="Fetch full JD per posting (SmartRecruiters/Workable only; slower, more requests).")
    pt.add_argument("--limit", type=int, default=10)

    pa = sub.add_parser("add", help="Add or update a company to poll.")
    pa.add_argument("--company", required=True)
    pa.add_argument("--platform", required=True, choices=PLATFORMS)
    pa.add_argument("--slug", required=True)
    pa.add_argument("--notes")
    pa.add_argument("--apply", action="store_true")

    sub.add_parser("list", help="List configured companies.")

    pp = sub.add_parser("poll", help="Fetch postings for configured companies and intake new ones.")
    pp.add_argument("--company-id")
    pp.add_argument("--with-details", action="store_true")
    pp.add_argument("--max-consecutive-failures", type=int, default=5)
    pp.add_argument("--apply", action="store_true")

    args = p.parse_args()
    print(f"===== ATS DISCOVERY ({DISCOVERY_VERSION}) =====")

    if args.command == "test":
        return cmd_test(None, args)

    with psycopg.connect(DSN, autocommit=False) as conn:
        return {
            "add": cmd_add, "list": cmd_list, "poll": cmd_poll,
        }[args.command](conn, args)


if __name__ == "__main__":
    sys.exit(main())
