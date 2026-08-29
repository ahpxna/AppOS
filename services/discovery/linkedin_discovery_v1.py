"""Persist bounded, user-initiated LinkedIn browser discovery results.

The browser executor may inspect a logged-in profile, but this database
boundary validates every returned JD before it becomes an applications row.
It never logs in, saves a LinkedIn job, messages anyone, or applies.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

from psycopg.types.json import Jsonb
from services.discovery.immigration_intelligence import record_jd_immigration_assessment
from services.intake.source_observation import observe_existing_posting

MAX_DISCOVERY_RESULTS = 5
MAX_SAVED_RESULTS = 20
MIN_JD_CHARS = 200
LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(?P<id>\d+)/?$")


class LinkedInDiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class LinkedInSearchSpec:
    keywords: str
    location: str
    max_results: int
    date_posted: str | None = None
    experience_levels: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    work_modes: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    sort_by: str = "recent"


def _terms(value: Any, *, limit: int = 8) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    cleaned = tuple(re.sub(r"\s+", " ", str(item).strip()) for item in values if str(item).strip())
    if len(cleaned) > limit or any(len(item) > 100 for item in cleaned):
        raise LinkedInDiscoveryError("search filter is too large.")
    return cleaned


def validate_search_request(keywords: str, location: str, max_results: int, **raw: Any) -> dict[str, Any]:
    keywords = re.sub(r"\s+", " ", (keywords or "").strip())
    location = re.sub(r"\s+", " ", (location or "").strip())
    if not keywords:
        raise LinkedInDiscoveryError("keywords are required.")
    if len(keywords) > 160 or len(location) > 160:
        raise LinkedInDiscoveryError("keywords/location are too long.")
    if not isinstance(max_results, int) or not 1 <= max_results <= MAX_DISCOVERY_RESULTS:
        raise LinkedInDiscoveryError(f"max_results must be 1..{MAX_DISCOVERY_RESULTS}.")
    date_posted = str(raw.get("date_posted") or "").casefold() or None
    if date_posted not in {None, "24h", "week", "month"}:
        raise LinkedInDiscoveryError("date_posted must be one of 24h, week, month.")
    sort_by = str(raw.get("sort_by") or "recent").casefold()
    if sort_by not in {"recent", "relevant"}:
        raise LinkedInDiscoveryError("sort_by must be recent or relevant.")
    spec = LinkedInSearchSpec(
        keywords, location, max_results, date_posted,
        _terms(raw.get("experience_levels")), _terms(raw.get("employment_types")),
        _terms(raw.get("work_modes")), _terms(raw.get("companies")), sort_by,
    )
    return {"keywords": spec.keywords, "location": spec.location, "max_results": spec.max_results,
            "date_posted": spec.date_posted, "experience_levels": list(spec.experience_levels),
            "employment_types": list(spec.employment_types), "work_modes": list(spec.work_modes),
            "companies": list(spec.companies), "sort_by": spec.sort_by}


def validate_saved_request(max_results: int) -> dict[str, Any]:
    """Validate the independently bounded, user-requested Saved Jobs read."""
    if not isinstance(max_results, int) or not 1 <= max_results <= MAX_SAVED_RESULTS:
        raise LinkedInDiscoveryError(f"max_results must be 1..{MAX_SAVED_RESULTS} for Saved Jobs.")
    return {"max_results": max_results}


def validate_job_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
            or not re.fullmatch(r"/jobs/view/\d+/?", parsed.path)):
        raise LinkedInDiscoveryError("result URL must be an https canonical LinkedIn /jobs/view/<id>/ page.")
    # Tracking parameters are neither evidence nor a stable identity.  Intake
    # keeps one canonical job URL for deduplication and later human review.
    return f"https://{parsed.hostname.lower()}{parsed.path.rstrip('/')}/"


def linkedin_job_id(url: str) -> str:
    """Return the LinkedIn posting identifier from one canonical job URL."""
    match = LINKEDIN_JOB_ID_RE.search(urlparse(validate_job_url(url)).path)
    if not match:
        raise LinkedInDiscoveryError("LinkedIn job URL has no stable posting id.")
    return match.group("id")


class BlockerSafeAgentResponse(dict):
    """Preserve grounded jobs while keeping blocker detection out of JD prose.

    The frozen LinkedIn handlers historically inspect ``str(response)`` or
    ``json.dumps(response)`` for CAPTCHA/login markers.  A legitimate JD can
    contain words such as "verification" or "security check", so those
    presentation forms expose only blocker/report metadata.  Normal dict access
    and ``values()`` still expose the complete response for ingestion.
    """

    def __init__(self, payload: dict[str, Any], blocker_view: dict[str, Any]):
        super().__init__(payload)
        self._blocker_view = blocker_view

    def __str__(self) -> str:
        return str(self._blocker_view)

    def __repr__(self) -> str:
        return repr(self._blocker_view)

    def items(self):
        # json.dumps(dict_subclass) uses items(); this intentionally keeps the
        # frozen Saved Jobs blocker scan away from JD text.
        return self._blocker_view.items()


def _contains_structured_jobs(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("jobs"), list):
            return True
        return any(_contains_structured_jobs(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_structured_jobs(v) for v in value)
    return False


def _structured_blocker_metadata(value: Any) -> dict[str, Any]:
    """Collect blocker fields from wrappers without scanning job-description prose."""
    found: dict[str, Any] = {}
    blocker_keys = ("blocked", "blocker", "error", "status", "warning")

    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            # Do not descend into the jobs array: JD text is evidence data and
            # can legitimately contain words such as verification/security.
            for key in blocker_keys:
                if key in node and key not in found:
                    found[key] = node[key]
            for key, nested in node.items():
                if key == "jobs":
                    continue
                if isinstance(nested, (dict, list)):
                    walk(nested, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for nested in node:
                if isinstance(nested, (dict, list)):
                    walk(nested, path)

    walk(value)
    return found


def blocker_safe_agent_response(value: Any) -> Any:
    """Wrap successful LinkedIn job output so JD prose cannot trigger blockers.

    Blocker metadata is commonly nested beneath the OpenClaw ``parsed`` wrapper.
    Preserve those fields in the safe string/JSON view while excluding the jobs
    array itself, so partial results plus a CAPTCHA cannot be misclassified as a
    clean extraction and ordinary JD prose cannot false-positive.
    """
    if not isinstance(value, dict) or not _contains_structured_jobs(value):
        return value
    blocker_view = _structured_blocker_metadata(value)
    blocker_view.setdefault("status", "jobs_extracted")
    return BlockerSafeAgentResponse(value, blocker_view)


def json_candidates(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from json_candidates(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from json_candidates(nested)
    elif isinstance(value, str):
        text = value.strip()
        token = chr(96) * 3
        fenced = re.search(re.escape(token) + r"(?:json)?\s*(.*?)\s*" + re.escape(token),
                           text, flags=re.I | re.S)
        parts = [fenced.group(1)] if fenced else []
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            parts.append(text[first:last + 1])
        for part in parts:
            try:
                yield json.loads(part)
            except json.JSONDecodeError:
                pass


def extract_jobs(agent_response: Any) -> list[dict[str, Any]]:
    for candidate in json_candidates(agent_response):
        if isinstance(candidate, dict) and isinstance(candidate.get("jobs"), list):
            return candidate["jobs"]
    raise LinkedInDiscoveryError("Browser agent returned no structured jobs array.")


def normalize_jobs(agent_response: Any, max_results: int) -> list[dict[str, str]]:
    raw_jobs = extract_jobs(agent_response)
    if len(raw_jobs) > max_results:
        raise LinkedInDiscoveryError("Browser agent exceeded the requested result cap.")
    rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        url = validate_job_url(str(item.get("url") or ""))
        company = re.sub(r"\s+", " ", str(item.get("company") or "").strip())[:300]
        title = re.sub(r"\s+", " ", str(item.get("title") or "").strip())[:500]
        location = re.sub(r"\s+", " ", str(item.get("location") or "").strip())[:300]
        work_mode = re.sub(r"\s+", " ", str(item.get("work_mode") or "").strip())[:100]
        jd_text = re.sub(r"\n{3,}", "\n\n", str(item.get("jd_text") or "").strip())
        if not company or not title or len(jd_text) < MIN_JD_CHARS or url in seen_urls:
            continue
        seen_urls.add(url)
        rows.append({"url": url, "company": company, "title": title, "location": location,
                     "work_mode": work_mode, "jd_text": jd_text})
    if not rows:
        raise LinkedInDiscoveryError(f"No valid JDs: need title/company/URL and {MIN_JD_CHARS}+ characters.")
    return rows


def ingest_discovered_jobs(cur, browser_task_id: str, search_input: dict[str, Any],
                           agent_response: Any) -> dict[str, Any]:
    request = validate_search_request(
        str(search_input.get("keywords") or ""), str(search_input.get("location") or ""),
        search_input.get("max_results"), date_posted=search_input.get("date_posted"),
        experience_levels=search_input.get("experience_levels"), employment_types=search_input.get("employment_types"),
        work_modes=search_input.get("work_modes"), companies=search_input.get("companies"),
        sort_by=search_input.get("sort_by"),
    )
    rows = normalize_jobs(agent_response, request["max_results"])
    created: list[str] = []
    duplicates = 0
    for row in rows:
        jd_hash = hashlib.sha256(row["jd_text"].encode("utf-8")).hexdigest()
        source_job_id = linkedin_job_id(row["url"])
        cur.execute("SELECT id::text FROM applications WHERE source = 'linkedin' AND source_job_id = %s;", (source_job_id,))
        existing = cur.fetchone()
        if existing:
            observe_existing_posting(
                cur, application_id=existing[0], source_name="linkedin", source_job_id=source_job_id,
                company=row["company"], job_title=row["title"], job_url=row["url"],
                jd_text=row["jd_text"], jd_hash=jd_hash, location=row["location"],
                work_mode=row["work_mode"], metadata={"discovery_channel": "search", "browser_task_id": browser_task_id},
            )
            cur.execute(
                "UPDATE applications SET discovery_channel='search', updated_at=now() WHERE id=%s;",
                (existing[0],),
            )
            duplicates += 1
            continue
        cur.execute(
            """INSERT INTO applications
                 (source, company, job_title, job_url, jd_text, jd_hash, current_step,
                  status, intake_channel, ats_type, location, work_mode, source_job_id,
                  discovery_channel,
                  first_seen_at, last_seen_at, created_at, updated_at)
               VALUES ('linkedin', %s, %s, %s, %s, %s, 'intake', 'active',
                       'linkedin_browser_discovery', 'linkedin_browser_linked_session',
                       %s, %s, %s, 'search', now(), now(), now(), now()) RETURNING id::text;""",
            (row["company"], row["title"], row["url"], row["jd_text"], jd_hash,
             row["location"], row["work_mode"], source_job_id),
        )
        application_id = cur.fetchone()[0]
        observe_existing_posting(
            cur, application_id=application_id, source_name="linkedin", source_job_id=source_job_id,
            company=row["company"], job_title=row["title"], job_url=row["url"],
            jd_text=row["jd_text"], jd_hash=jd_hash, location=row["location"],
            work_mode=row["work_mode"], metadata={"discovery_channel": "search", "browser_task_id": browser_task_id, "initial": True},
        )
        immigration = record_jd_immigration_assessment(cur, application_id, row["jd_text"])
        cur.execute(
            """INSERT INTO pipeline_events
                 (application_id, from_step, to_step, actor, reason, detail_json)
               VALUES (%s, NULL, 'intake', 'linkedin_browser_discovery',
                       'User-initiated bounded LinkedIn discovery capture.', %s);""",
            (application_id, Jsonb({"browser_task_id": browser_task_id,
                "keywords": request["keywords"], "location": request["location"],
                "max_results": request["max_results"], "job_url": row["url"],
                "immigration_assessment": immigration})),
        )
        created.append(application_id)
    return {"requested_max_results": request["max_results"], "returned_valid_jobs": len(rows),
            "created_application_ids": created, "duplicates": duplicates}


def ingest_saved_jobs(cur, browser_task_id: str, saved_input: dict[str, Any],
                      agent_response: Any) -> dict[str, Any]:
    """Ingest existing LinkedIn Saved Jobs through the normal application boundary.

    This only persists already-saved job records returned by the read-only
    browser task; it never changes a LinkedIn saved state.
    """
    request = validate_saved_request(saved_input.get("max_results"))
    sync_id = str(saved_input.get("saved_sync_id") or "").strip() or None
    rows = normalize_jobs(agent_response, request["max_results"])
    created: list[str] = []
    duplicates = 0
    for row in rows:
        jd_hash = hashlib.sha256(row["jd_text"].encode("utf-8")).hexdigest()
        source_job_id = linkedin_job_id(row["url"])
        cur.execute("SELECT id::text FROM applications WHERE source = 'linkedin' AND source_job_id = %s;", (source_job_id,))
        existing = cur.fetchone()
        if existing:
            observe_existing_posting(
                cur, application_id=existing[0], source_name="linkedin", source_job_id=source_job_id,
                company=row["company"], job_title=row["title"], job_url=row["url"],
                jd_text=row["jd_text"], jd_hash=jd_hash, location=row["location"],
                work_mode=row["work_mode"], metadata={"discovery_channel": "saved", "saved_sync_id": sync_id, "browser_task_id": browser_task_id},
            )
            cur.execute(
                """UPDATE applications SET discovery_channel='saved', linkedin_saved_at=now(),
                         linkedin_saved_sync_id=%s, updated_at=now() WHERE id=%s;""",
                (sync_id, existing[0]),
            )
            duplicates += 1
            continue
        cur.execute(
            """INSERT INTO applications
                 (source, company, job_title, job_url, jd_text, jd_hash, current_step,
                  status, intake_channel, ats_type, location, work_mode, source_job_id,
                  discovery_channel, linkedin_saved_at, linkedin_saved_sync_id,
                  first_seen_at, last_seen_at, created_at, updated_at)
               VALUES ('linkedin', %s, %s, %s, %s, %s, 'intake', 'active',
                       'linkedin_saved_jobs', 'linkedin_browser_linked_session', %s, %s, %s,
                       'saved', now(), %s, now(), now(), now(), now()) RETURNING id::text;""",
            (row["company"], row["title"], row["url"], row["jd_text"], jd_hash,
             row["location"], row["work_mode"], source_job_id, sync_id),
        )
        application_id = cur.fetchone()[0]
        observe_existing_posting(
            cur, application_id=application_id, source_name="linkedin", source_job_id=source_job_id,
            company=row["company"], job_title=row["title"], job_url=row["url"],
            jd_text=row["jd_text"], jd_hash=jd_hash, location=row["location"],
            work_mode=row["work_mode"], metadata={"discovery_channel": "saved", "saved_sync_id": sync_id, "browser_task_id": browser_task_id, "initial": True},
        )
        immigration = record_jd_immigration_assessment(cur, application_id, row["jd_text"])
        cur.execute(
            """INSERT INTO pipeline_events
                 (application_id, from_step, to_step, actor, reason, detail_json)
               VALUES (%s, NULL, 'intake', 'linkedin_saved_jobs',
                       'User-initiated read-only LinkedIn Saved Jobs capture.', %s);""",
            (application_id, Jsonb({"browser_task_id": browser_task_id, "saved_sync_id": sync_id,
                "max_results": request["max_results"], "job_url": row["url"],
                "immigration_assessment": immigration, "discovery_channel": "saved"})),
        )
        created.append(application_id)
    if sync_id:
        cur.execute(
            """UPDATE linkedin_saved_syncs SET status = 'completed', jobs_seen = %s,
                      jobs_created = %s, duplicates = %s, completed_at = now(), error_message = NULL
                 WHERE id = %s;""",
            (len(rows), len(created), duplicates, sync_id),
        )
    return {"requested_max_results": request["max_results"], "returned_valid_jobs": len(rows),
            "created_application_ids": created, "duplicates": duplicates, "saved_sync_id": sync_id}
