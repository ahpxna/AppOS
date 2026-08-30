#!/usr/bin/env python3
"""Profile/preferences-driven autonomous discovery planner.

The planner owns *scheduling only*.  Browser execution remains in the existing
browser worker and ATS fetching remains in ``ats_discovery_v1``.  Every queued
LinkedIn search is bounded, bucket-idempotent and preference-aware; the planner
never invents ATS tenant slugs or bypasses application/review/submit gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn
from services.common.profile_job_matching import unique_terms
from services.discovery.linkedin_discovery_v1 import validate_search_request
from services.discovery.linkedin_intake_v1 import queue_discovery_task, queue_saved_sync_task
from services.discovery.ats_source_enrollment_v1 import enroll_ats_source
from services.discovery.profile_job_search_v1 import approved_terms


PLANNER_KEY = "profile_autonomous_discovery_v1"
_REGEX_META = re.compile(r"[\\.^$*+?{}\[\]|()]")


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(value, high))


def _preferences(cur) -> dict[str, Any]:
    cur.execute("SELECT * FROM job_search_preferences WHERE profile_key='primary';")
    row = cur.fetchone()
    return dict(zip([column.name for column in cur.description], row or ()))


def _literal_locations(value: Any) -> list[str]:
    """Use only literal-safe allow patterns as LinkedIn location inputs.

    Regex preferences remain valid post-intake filters, but feeding regex syntax
    to LinkedIn as a location would change its meaning.  If no literal location
    exists the search intentionally uses LinkedIn's unscoped location.
    """
    result: list[str] = []
    for item in value or []:
        text = re.sub(r"\s+", " ", str(item).strip())
        if text and len(text) <= 160 and not _REGEX_META.search(text):
            result.append(text)
    return unique_terms(result)[:4]


def _date_posted(freshness_days: Any) -> str:
    try:
        days = max(1, int(freshness_days or 30))
    except (TypeError, ValueError):
        days = 30
    if days <= 1:
        return "24h"
    if days <= 7:
        return "week"
    return "month"


def _bucket(now: float, cooldown_seconds: int) -> int:
    return int(now // max(3600, cooldown_seconds))


def _fingerprint(request: dict[str, Any]) -> str:
    raw = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_linkedin_plan(cur, *, now: float | None = None) -> list[dict[str, Any]]:
    """Build a rotating bounded search plan from approved profile + preferences."""
    prefs = _preferences(cur)
    terms = unique_terms(approved_terms(cur))
    if not terms:
        return []
    cooldown = _bounded_int("JOBOS_LINKEDIN_DISCOVERY_COOLDOWN_SECONDS", 21600, 3600, 604800)
    cycle = _bucket(time.time() if now is None else now, cooldown)
    locations = _literal_locations(prefs.get("location_allow_patterns")) or [""]
    work_modes = [str(x).strip() for x in (prefs.get("allowed_work_modes") or []) if str(x).strip()][:8]
    employment = [str(x).strip() for x in (prefs.get("allowed_employment_types") or []) if str(x).strip()][:8]
    max_results = _bounded_int("JOBOS_LINKEDIN_DISCOVERY_MAX_RESULTS", 3, 1, 5)

    candidates: list[dict[str, Any]] = []
    for term in terms:
        for location in locations:
            candidates.append(validate_search_request(
                term, location, max_results,
                date_posted=_date_posted(prefs.get("freshness_days")),
                employment_types=employment, work_modes=work_modes, sort_by="recent",
            ))
    if not candidates:
        return []
    # Rotate the search window by cooldown bucket so large profiles eventually
    # receive coverage without creating dozens of browser tasks in one cycle.
    per_cycle = _bounded_int("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", 3, 1, 10)
    start = (cycle * per_cycle) % len(candidates)
    ordered = candidates[start:] + candidates[:start]
    return ordered[: min(per_cycle, len(candidates))]


def _enroll_observed_ats_sources(cur, *, limit: int = 200) -> int:
    """Project already-grounded application/source URLs into ATS polling sources."""
    candidates: list[tuple[str, str]] = []
    cur.execute(
        """SELECT coalesce(company,''),coalesce(job_url,'')
             FROM applications
            WHERE nullif(trim(coalesce(job_url,'')),'') IS NOT NULL
            ORDER BY updated_at DESC LIMIT %s;""",
        (max(1, min(int(limit), 1000)),),
    )
    candidates.extend((str(company or ""), str(url or "")) for company, url in cur.fetchall())
    cur.execute(
        """SELECT coalesce(company,''),coalesce(metadata_json->>'external_apply_url','')
             FROM job_posting_source_revisions
            WHERE nullif(trim(coalesce(metadata_json->>'external_apply_url','')),'') IS NOT NULL
            ORDER BY observed_at DESC LIMIT %s;""",
        (max(1, min(int(limit), 1000)),),
    )
    candidates.extend((str(company or ""), str(url or "")) for company, url in cur.fetchall())
    enrolled: set[str] = set()
    for company, url in candidates:
        source_id = enroll_ats_source(
            cur, company=company, apply_url=url, evidence_source="observed_job_source",
        )
        if source_id:
            enrolled.add(source_id)
    return len(enrolled)


def _active_planner_tasks(cur) -> int:
    cur.execute(
        """SELECT count(*) FROM browser_tasks
             WHERE requested_by=%s AND status IN ('queued','running');""",
        (PLANNER_KEY,),
    )
    return int(cur.fetchone()[0])


def run_once(cur, *, apply: bool, now: float | None = None) -> dict[str, Any]:
    now_value = time.time() if now is None else float(now)
    cooldown = _bounded_int("JOBOS_LINKEDIN_DISCOVERY_COOLDOWN_SECONDS", 21600, 3600, 604800)
    cycle = _bucket(now_value, cooldown)
    max_queued = _bounded_int("JOBOS_DISCOVERY_MAX_QUEUED_TASKS", 6, 1, 50)
    ats_sources_enrolled = _enroll_observed_ats_sources(cur) if apply else 0
    active = _active_planner_tasks(cur)
    slots = max(0, max_queued - active)
    planned = build_linkedin_plan(cur, now=now_value)
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    linkedin_enabled = _truthy("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED", False)
    if linkedin_enabled:
        for request in planned:
            if slots <= 0:
                skipped.append({"reason": "backpressure", "search": request})
                continue
            key = f"jobos:auto-linkedin:{cycle}:{_fingerprint(request)}"
            if apply:
                task_id, created = queue_discovery_task(
                    cur, request=request,
                    timeout=_bounded_int("JOBOS_LINKEDIN_DISCOVERY_TIMEOUT_SECONDS", 300, 60, 1800),
                    requested_by=PLANNER_KEY, autonomous=True, idempotency_key=key,
                )
            else:
                task_id, created = "dry-run", True
            queued.append({"browser_task_id": task_id, "created": created,
                           "idempotency_key": key, "search": request})
            if created:
                slots -= 1
    elif planned:
        skipped.append({"reason": "linkedin_discovery_disabled", "planned_searches": len(planned)})

    saved_result: dict[str, Any] | None = None
    if _truthy("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED", False):
        saved_cooldown = _bounded_int("JOBOS_LINKEDIN_SAVED_SYNC_COOLDOWN_SECONDS", 21600, 3600, 604800)
        saved_bucket = _bucket(now_value, saved_cooldown)
        saved_key = f"jobos:auto-linkedin-saved:{saved_bucket}"
        if slots <= 0:
            saved_result = {"created": False, "reason": "backpressure", "idempotency_key": saved_key}
        elif apply:
            sync_id, task_id, created = queue_saved_sync_task(
                cur,
                max_results=_bounded_int("JOBOS_LINKEDIN_SAVED_MAX_RESULTS", 10, 1, 20),
                timeout=_bounded_int("JOBOS_LINKEDIN_SAVED_TIMEOUT_SECONDS", 600, 60, 1800),
                requested_by=PLANNER_KEY, autonomous=True, idempotency_key=saved_key,
            )
            saved_result = {"saved_sync_id": sync_id, "browser_task_id": task_id,
                            "created": created, "idempotency_key": saved_key}
        else:
            saved_result = {"saved_sync_id": "dry-run", "browser_task_id": "dry-run",
                            "created": True, "idempotency_key": saved_key}

    cur.execute("SELECT count(*) FROM ats_companies WHERE enabled=true;")
    ats_companies = int(cur.fetchone()[0])
    return {
        "apply": bool(apply), "cycle": cycle, "approved_term_count": len(approved_terms(cur)),
        "linkedin_enabled": linkedin_enabled, "active_planner_tasks_before": active,
        "queued_searches": queued, "skipped": skipped, "saved_sync": saved_result,
        "ats_companies_enabled": ats_companies, "ats_sources_enrolled": ats_sources_enrolled,
        "ats_note": ("ATS periodic polling has configured sources." if ats_companies else
                     "No grounded ATS source yet; LinkedIn external apply URLs can auto-enroll deterministic ATS sources."),
    }


def status(cur) -> dict[str, Any]:
    cur.execute(
        """SELECT status,count(*) FROM browser_tasks
             WHERE requested_by=%s GROUP BY status ORDER BY status;""",
        (PLANNER_KEY,),
    )
    tasks = {str(status): int(count) for status, count in cur.fetchall()}
    cur.execute("SELECT count(*) FROM ats_companies WHERE enabled=true;")
    ats_count = int(cur.fetchone()[0])
    return {
        "planner": PLANNER_KEY,
        "linkedin_enabled": _truthy("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED", False),
        "saved_enabled": _truthy("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED", False),
        "approved_profile_terms": approved_terms(cur),
        "planner_browser_tasks": tasks,
        "enabled_ats_companies": ats_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Plan one bounded autonomous discovery cycle.")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Queue due discovery work.")
    mode.add_argument("--dry-run", action="store_true", help="Print plan only (default).")
    sub.add_parser("status", help="Show profile terms, planner tasks and ATS source count.")
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        if args.command == "status":
            print(json.dumps(status(cur), default=str, indent=2))
            conn.rollback()
            return 0
        result = run_once(cur, apply=bool(args.apply))
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps(result, default=str, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
