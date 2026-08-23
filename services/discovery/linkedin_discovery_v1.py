"""Persist bounded, user-initiated LinkedIn browser discovery results.

The browser executor may inspect a logged-in profile, but this database
boundary validates every returned JD before it becomes an applications row.
It never logs in, saves a LinkedIn job, messages anyone, or applies.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from psycopg.types.json import Jsonb
from services.discovery.immigration_intelligence import record_jd_immigration_assessment

MAX_DISCOVERY_RESULTS = 5
MIN_JD_CHARS = 200


class LinkedInDiscoveryError(ValueError):
    pass


def validate_search_request(keywords: str, location: str, max_results: int) -> dict[str, Any]:
    keywords = re.sub(r"\s+", " ", (keywords or "").strip())
    location = re.sub(r"\s+", " ", (location or "").strip())
    if not keywords:
        raise LinkedInDiscoveryError("keywords are required.")
    if len(keywords) > 160 or len(location) > 160:
        raise LinkedInDiscoveryError("keywords/location are too long.")
    if not isinstance(max_results, int) or not 1 <= max_results <= MAX_DISCOVERY_RESULTS:
        raise LinkedInDiscoveryError(f"max_results must be 1..{MAX_DISCOVERY_RESULTS}.")
    return {"keywords": keywords, "location": location, "max_results": max_results}


def validate_job_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if (parsed.scheme != "https" or not (parsed.hostname or "").lower().endswith("linkedin.com")
            or not parsed.path.startswith("/jobs/")):
        raise LinkedInDiscoveryError("result URL must be an https LinkedIn /jobs/ page.")
    return parsed.geturl()


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
    request = validate_search_request(str(search_input.get("keywords") or ""),
                                      str(search_input.get("location") or ""),
                                      search_input.get("max_results"))
    rows = normalize_jobs(agent_response, request["max_results"])
    created: list[str] = []
    duplicates = 0
    for row in rows:
        jd_hash = hashlib.sha256(row["jd_text"].encode("utf-8")).hexdigest()
        cur.execute("SELECT id::text FROM applications WHERE jd_hash = %s;", (jd_hash,))
        if cur.fetchone():
            duplicates += 1
            continue
        cur.execute(
            """INSERT INTO applications
                 (source, company, job_title, job_url, jd_text, jd_hash, current_step,
                  status, intake_channel, ats_type, location, work_mode, created_at, updated_at)
               VALUES ('linkedin', %s, %s, %s, %s, %s, 'intake', 'active',
                       'linkedin_browser_discovery', 'linkedin_browser_linked_session',
                       %s, %s, now(), now()) RETURNING id::text;""",
            (row["company"], row["title"], row["url"], row["jd_text"], jd_hash,
             row["location"], row["work_mode"]),
        )
        application_id = cur.fetchone()[0]
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
