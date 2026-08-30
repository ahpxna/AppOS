#!/usr/bin/env python3
"""Profile/preferences-driven autonomous discovery planner.

The planner owns *scheduling only*.  Browser execution remains in the existing
browser worker and ATS fetching remains in ``ats_discovery_v1``.  Every queued
LinkedIn search is bounded, rolling-cooldown idempotent and preference-aware; the planner
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
import math
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn
from services.common.profile_job_matching import unique_terms
from services.discovery.linkedin_discovery_v1 import validate_search_request
from services.discovery.linkedin_intake_v1 import queue_discovery_task, queue_saved_sync_task, safe_discovery_reissue
from services.discovery.ats_source_enrollment_v1 import enroll_ats_source
from services.discovery.profile_job_search_v1 import approved_terms


PLANNER_KEY = "profile_autonomous_discovery_v1"
SEARCH_STATE_PREFIX = PLANNER_KEY + ":search:"
SAVED_STATE_KEY = PLANNER_KEY + ":saved"
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
    """Translate only provably literal location preferences into LinkedIn inputs.

    Plain values remain unchanged. A small exact-alternation regex such as
    ``^(Boston|New York)$`` can be expanded safely; open-ended regexes such as
    ``Boston.*`` remain post-intake filters and use the wider unscoped pool.
    """
    result: list[str] = []
    for item in value or []:
        text = re.sub(r"\s+", " ", str(item).strip())
        if not text or len(text) > 160:
            continue
        if not _REGEX_META.search(text):
            result.append(text)
            continue
        candidate = text
        if candidate.startswith("^"):
            candidate = candidate[1:]
        if candidate.endswith("$"):
            candidate = candidate[:-1]
        if candidate.startswith("(?:") and candidate.endswith(")"):
            candidate = candidate[3:-1]
        elif candidate.startswith("(") and candidate.endswith(")"):
            candidate = candidate[1:-1]
        branches = [part.strip() for part in candidate.split("|")]
        if len(branches) > 1 and all(
            branch and len(branch) <= 160 and re.fullmatch(r"[A-Za-z0-9 .,'/&()_-]+", branch)
            for branch in branches
        ):
            result.extend(branches)
    return unique_terms(result)[:8]


def _prioritized_terms(cur) -> list[str]:
    """Prefer role/capability terms before generic tool/competency tags."""
    if not hasattr(cur, "execute"):
        return unique_terms(approved_terms(cur))
    cur.execute(
        """WITH weighted(term,priority) AS (
             SELECT capability_name,0 FROM profile_capabilities WHERE status='approved'
             UNION ALL SELECT unnest(role_families),1 FROM profile_capabilities WHERE status='approved'
             UNION ALL SELECT unnest(tool_tags),2 FROM profile_capabilities WHERE status='approved'
             UNION ALL SELECT unnest(competency_tags),3 FROM profile_capabilities WHERE status='approved'
           )
           SELECT lower(btrim(term)),min(priority) FROM weighted
            WHERE length(btrim(term)) BETWEEN 2 AND 100
            GROUP BY lower(btrim(term))
            ORDER BY min(priority),lower(btrim(term));"""
    )
    return unique_terms(row[0] for row in cur.fetchall())


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


def _fingerprint(request: dict[str, Any]) -> str:
    raw = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_linkedin_plan(cur, *, now: float | None = None) -> list[dict[str, Any]]:
    """Build a rotating bounded search plan from approved profile + preferences."""
    prefs = _preferences(cur)
    terms = _prioritized_terms(cur)
    if not terms:
        return []
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
    # Durable state (not epoch buckets) selects the next high-priority coverage
    # window. Regex-only locations intentionally widen the result pool, then
    # remain a strict post-intake filter instead of silently searching only a
    # few global rows.
    # Cover the full term/location matrix within a bounded target window instead
    # of hard-coding three searches per six hours.  The per-query rolling
    # cooldown below still prevents repeated searches; this only controls how
    # quickly the durable cursor reaches every distinct candidate.
    interval_seconds = _bounded_int("JOBOS_PROFILE_DISCOVERY_INTERVAL_SECONDS", 900, 60, 86400)
    target_hours = _bounded_int("JOBOS_LINKEDIN_TARGET_COVERAGE_HOURS", 24, 1, 168)
    cycles_in_target = max(1, int((target_hours * 3600) / interval_seconds))
    adaptive = max(1, math.ceil(len(candidates) / cycles_in_target))
    configured_floor = _bounded_int("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", 3, 1, 20)
    per_cycle = min(20, max(configured_floor, adaptive))
    if locations == [""] and prefs.get("location_allow_patterns"):
        # Regex-only locations cannot be faithfully translated to LinkedIn's
        # location input. Widen the autonomous read pool, then enforce the exact
        # regex after intake. Manual discovery retains its smaller cap.
        max_results = _bounded_int("JOBOS_LINKEDIN_REGEX_LOCATION_MAX_RESULTS", 20, 3, 20)
        candidates = [{**item, "max_results": max_results} for item in candidates]
    if hasattr(cur, "execute"):
        cur.execute("SELECT cursor FROM discovery_scheduler_state WHERE scheduler_key=%s;", (PLANNER_KEY,))
        row = cur.fetchone()
    else:  # pure planner tests / dry plan consumers without a DB cursor
        row = None
    start = int(row[0] or 0) % len(candidates) if row else 0
    ordered = candidates[start:] + candidates[:start]
    return ordered[: min(per_cycle, len(candidates))]


def _coverage_requirements(cur) -> dict[str, int]:
    """Describe matrix coverage independently from current queue occupancy."""
    prefs = _preferences(cur)
    terms = _prioritized_terms(cur)
    locations = _literal_locations(prefs.get("location_allow_patterns")) or ([""] if terms else [])
    matrix_size = len(terms) * len(locations)
    interval_seconds = _bounded_int("JOBOS_PROFILE_DISCOVERY_INTERVAL_SECONDS", 900, 60, 86400)
    target_hours = _bounded_int("JOBOS_LINKEDIN_TARGET_COVERAGE_HOURS", 24, 1, 168)
    cycles_in_target = max(1, int((target_hours * 3600) / interval_seconds))
    required = math.ceil(matrix_size / cycles_in_target) if matrix_size else 0
    configured_floor = _bounded_int("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", 3, 1, 20)
    return {
        "matrix_size": matrix_size,
        "target_hours": target_hours,
        "cycles_in_target": cycles_in_target,
        "required_searches_per_cycle": min(20, max(configured_floor, required)) if matrix_size else 0,
    }


def _enroll_observed_ats_sources(cur, *, limit: int = 200) -> int:
    """Project only DOM-href-evidenced external URLs into ATS sources."""
    candidates: list[tuple[str, str, str, str]] = []
    cur.execute(
        """SELECT coalesce(company,''),coalesce(metadata_json->>'external_apply_url',''),
                  coalesce(metadata_json->>'external_apply_href_evidence',''),
                  coalesce(metadata_json->>'external_apply_company_evidence','')
             FROM job_posting_source_revisions
            WHERE nullif(trim(coalesce(metadata_json->>'external_apply_url','')),'') IS NOT NULL
              AND metadata_json->>'external_apply_evidence_authority' = 'browser_dom_job_company_apply_v2'
              AND metadata_json->>'external_apply_href_evidence' = metadata_json->>'external_apply_url'
              AND lower(trim(coalesce(metadata_json->>'external_apply_company_evidence',''))) = lower(trim(company))
            ORDER BY observed_at DESC LIMIT %s;""",
        (max(1, min(int(limit), 1000)),),
    )
    candidates.extend((str(company or ""), str(url or ""), str(witness or ""), str(company_witness or ""))
                      for company, url, witness, company_witness in cur.fetchall())
    enrolled: set[str] = set()
    for company, url, witness, company_witness in candidates:
        source_id = enroll_ats_source(
            cur, company=company, apply_url=url, href_evidence=witness,
            company_evidence=company_witness, evidence_source="linkedin_observed_job_source",
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


def _ensure_scheduler_state(cur, key: str) -> tuple[int, Any, Any]:
    cur.execute(
        """INSERT INTO discovery_scheduler_state(scheduler_key,cursor,updated_at)
             VALUES (%s,0,now()) ON CONFLICT (scheduler_key) DO NOTHING;""",
        (key,),
    )
    cur.execute(
        "SELECT cursor,last_queued_at,last_finished_at FROM discovery_scheduler_state WHERE scheduler_key=%s FOR UPDATE;",
        (key,),
    )
    row = cur.fetchone()
    return (int(row[0] or 0), row[1], row[2]) if row else (0, None, None)


def _latest_task(cur, base_key: str, task_type: str) -> tuple[str, str, str, Any] | None:
    cur.execute(
        """SELECT idempotency_key,status,coalesce(error_message,''),finished_at
             FROM browser_tasks
            WHERE requested_by=%s AND task_type=%s
              AND (idempotency_key=%s OR idempotency_key LIKE %s)
            ORDER BY created_at DESC LIMIT 1;""",
        (PLANNER_KEY, task_type, base_key, base_key + ':%'),
    )
    row = cur.fetchone()
    return (str(row[0]), str(row[1]), str(row[2] or ''), row[3]) if row else None


def _cooldown_anchor(last_queued_at: Any, last_finished_at: Any) -> Any:
    """Cooldown starts from completion when known, otherwise from queue time.

    Using queue time alone shortens the effective cooldown by however long a
    browser task ran.  ``max`` also protects a newly queued occurrence while a
    previous completion timestamp is still present in scheduler state.
    """
    values = [value for value in (last_queued_at, last_finished_at) if value is not None]
    return max(values) if values else None


def _occurrence_key(base_key: str, now_value: float) -> str:
    # Rolling cooldown is authoritative; the timestamp only makes each due
    # occurrence distinct under browser_tasks' global idempotency constraint.
    return f"{base_key}:{int(now_value * 1000)}"


def _failed_task_action(
    cur, *, base_key: str, task_type: str, now_value: float,
) -> tuple[str | None, str | None]:
    """Return (idempotency_key, block_reason) for the latest failed occurrence.

    Retryable/auth-session failures may reuse the exact durable task identity
    after a short backoff.  Non-retryable OpenClaw/config/policy failures must
    *not* silently start a fresh occurrence after the ordinary search cooldown;
    autonomous discovery remains blocked until an operator explicitly repairs
    the configuration and manually exercises the feature.
    """
    latest = _latest_task(cur, base_key, task_type)
    if not latest or latest[1] != "failed":
        return None, None
    if not safe_discovery_reissue(latest[2]):
        # A non-retryable autonomous failure (bad OpenClaw credentials/policy/
        # configuration) must not churn forever.  Once an operator explicitly
        # exercises the same discovery task type successfully, that durable
        # success is evidence that the external/config boundary was repaired
        # and autonomous scheduling may resume.
        failed_finished_at = latest[3]
        if failed_finished_at is not None:
            cur.execute(
                """SELECT 1 FROM browser_tasks
                    WHERE task_type=%s
                      AND requested_by<>%s
                      AND status='completed'
                      AND finished_at>%s
                    LIMIT 1;""",
                (task_type, PLANNER_KEY, failed_finished_at),
            )
            if cur.fetchone():
                return None, None
        return None, "non_retryable_failure"
    retry_delay = _bounded_int("JOBOS_LINKEDIN_FAILED_RETRY_COOLDOWN_SECONDS", 300, 60, 3600)
    finished_at = latest[3]
    if finished_at is not None and now_value - finished_at.timestamp() < retry_delay:
        return None, "retry_backoff"
    return latest[0], None


def _retryable_failed_key(cur, *, base_key: str, task_type: str, now_value: float) -> str | None:
    """Backward-compatible projection for tests/tools."""
    key, _reason = _failed_task_action(
        cur, base_key=base_key, task_type=task_type, now_value=now_value
    )
    return key


def run_once(cur, *, apply: bool, now: float | None = None) -> dict[str, Any]:
    now_value = time.time() if now is None else float(now)
    cooldown = _bounded_int("JOBOS_LINKEDIN_DISCOVERY_COOLDOWN_SECONDS", 21600, 3600, 604800)
    max_queued = _bounded_int("JOBOS_DISCOVERY_MAX_QUEUED_TASKS", 6, 1, 50)
    if apply:
        # One planner transaction owns cursor/cooldown decisions at a time.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (PLANNER_KEY,))
    ats_sources_enrolled = _enroll_observed_ats_sources(cur) if apply else 0
    active = _active_planner_tasks(cur)
    slots = max(0, max_queued - active)
    if apply:
        current_cursor, _, _ = _ensure_scheduler_state(cur, PLANNER_KEY)
    else:
        cur.execute("SELECT cursor FROM discovery_scheduler_state WHERE scheduler_key=%s;", (PLANNER_KEY,))
        cursor_row = cur.fetchone()
        current_cursor = int(cursor_row[0] or 0) if cursor_row else 0

    linkedin_capable = _truthy("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED", False)
    autonomous_enabled = _truthy("JOBOS_AUTONOMOUS_DISCOVERY_ENABLED", False)
    saved_enabled = _truthy("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED", False)
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Saved Jobs is an explicit user signal. Reserve the first available slot
    # before ordinary matrix searches so a large search matrix cannot starve it.
    saved_result: dict[str, Any] | None = None
    if saved_enabled and autonomous_enabled:
        saved_cooldown = _bounded_int("JOBOS_LINKEDIN_SAVED_SYNC_COOLDOWN_SECONDS", 21600, 3600, 604800)
        saved_base_key = "jobos:auto-linkedin-saved"
        if slots <= 0:
            saved_result = {"created": False, "reason": "backpressure", "idempotency_key": saved_base_key}
        elif apply:
            _, saved_last_queued, saved_last_finished = _ensure_scheduler_state(cur, SAVED_STATE_KEY)
            retry_key, failure_block = _failed_task_action(
                cur, base_key=saved_base_key, task_type="discover_linkedin_saved_jobs", now_value=now_value
            )
            if failure_block:
                saved_result = {"created": False, "reason": failure_block, "idempotency_key": saved_base_key}
                saved_key = ""
            elif retry_key:
                saved_key = retry_key
            elif (saved_anchor := _cooldown_anchor(saved_last_queued, saved_last_finished)) and now_value - saved_anchor.timestamp() < saved_cooldown:
                saved_result = {"created": False, "reason": "rolling_cooldown", "idempotency_key": saved_base_key}
                saved_key = ""
            else:
                saved_key = _occurrence_key(saved_base_key, now_value)
            if saved_key:
                sync_id, task_id, created = queue_saved_sync_task(
                    cur, max_results=_bounded_int("JOBOS_LINKEDIN_SAVED_MAX_RESULTS", 10, 1, 20),
                    timeout=_bounded_int("JOBOS_LINKEDIN_SAVED_TIMEOUT_SECONDS", 600, 60, 1800),
                    requested_by=PLANNER_KEY, autonomous=True, idempotency_key=saved_key,
                )
                if created:
                    cur.execute(
                        "UPDATE discovery_scheduler_state SET last_queued_at=now(),updated_at=now() WHERE scheduler_key=%s;",
                        (SAVED_STATE_KEY,),
                    )
                    slots -= 1
                saved_result = {"saved_sync_id": sync_id, "browser_task_id": task_id,
                                "created": created, "idempotency_key": saved_key}
        else:
            saved_key = _occurrence_key(saved_base_key, now_value)
            saved_result = {"saved_sync_id": "dry-run", "browser_task_id": "dry-run",
                            "created": True, "idempotency_key": saved_key}
            slots = max(0, slots - 1)

    coverage = _coverage_requirements(cur)
    nominal_search_capacity = max_queued - (1 if saved_enabled and autonomous_enabled else 0)
    coverage["configured_search_queue_capacity"] = max(0, nominal_search_capacity)
    coverage["available_search_slots_this_cycle"] = max(0, slots)
    coverage["target_achievable_from_queue_capacity"] = (
        coverage["required_searches_per_cycle"] <= max(0, nominal_search_capacity)
    )
    if not coverage["target_achievable_from_queue_capacity"]:
        coverage["capacity_shortfall"] = (
            coverage["required_searches_per_cycle"] - max(0, nominal_search_capacity)
        )

    # Queue admission is clamped by ``slots`` below, but the candidate scan must
    # remain wider than the admission budget.  Otherwise a few leading
    # cooldown/permanent-block entries can leave capacity idle even though later
    # matrix entries are due.  Coverage feasibility is reported from physical
    # queue capacity above rather than pretending every planned candidate can be
    # admitted.
    planned = build_linkedin_plan(cur, now=now_value)
    considered = 0
    if linkedin_capable and autonomous_enabled:
        for request in planned:
            if slots <= 0:
                break
            considered += 1
            fingerprint = _fingerprint(request)
            state_key = SEARCH_STATE_PREFIX + fingerprint
            base_key = f"jobos:auto-linkedin:{fingerprint}"
            if apply:
                _, last_queued_at, last_finished_at = _ensure_scheduler_state(cur, state_key)
                retry_key, failure_block = _failed_task_action(
                    cur, base_key=base_key, task_type="discover_linkedin_jobs", now_value=now_value
                )
                if failure_block:
                    skipped.append({"reason": failure_block, "search": request})
                    continue
                if retry_key:
                    key = retry_key
                elif (cooldown_anchor := _cooldown_anchor(last_queued_at, last_finished_at)) and (now_value - cooldown_anchor.timestamp()) < cooldown:
                    skipped.append({"reason": "rolling_cooldown", "search": request})
                    continue
                else:
                    key = _occurrence_key(base_key, now_value)
                task_id, created = queue_discovery_task(
                    cur, request=request,
                    timeout=_bounded_int("JOBOS_LINKEDIN_DISCOVERY_TIMEOUT_SECONDS", 300, 60, 1800),
                    requested_by=PLANNER_KEY, autonomous=True, idempotency_key=key,
                )
                if created:
                    cur.execute(
                        "UPDATE discovery_scheduler_state SET last_queued_at=now(),updated_at=now() WHERE scheduler_key=%s;",
                        (state_key,),
                    )
            else:
                key, task_id, created = _occurrence_key(base_key, now_value), "dry-run", True
            queued.append({"browser_task_id": task_id, "created": created,
                           "idempotency_key": key, "search": request})
            if created:
                slots -= 1
        if apply and considered:
            cur.execute(
                "UPDATE discovery_scheduler_state SET cursor=%s,updated_at=now() WHERE scheduler_key=%s;",
                (current_cursor + considered, PLANNER_KEY),
            )
    elif planned:
        skipped.append({"reason": "autonomous_discovery_disabled" if linkedin_capable else "linkedin_capability_disabled",
                        "planned_searches": len(planned)})

    cur.execute("SELECT count(*) FROM ats_companies WHERE enabled=true;")
    ats_companies = int(cur.fetchone()[0])
    return {
        "apply": bool(apply), "approved_term_count": len(approved_terms(cur)),
        "linkedin_enabled": linkedin_capable, "autonomous_enabled": autonomous_enabled,
        "active_planner_tasks_before": active,
        "queued_searches": queued, "skipped": skipped, "saved_sync": saved_result,
        "coverage": coverage,
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
        "autonomous_enabled": _truthy("JOBOS_AUTONOMOUS_DISCOVERY_ENABLED", False),
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
