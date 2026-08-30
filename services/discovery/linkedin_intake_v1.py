#!/usr/bin/env python3
"""User-initiated LinkedIn intake, bounded to pasted/exported jobs.

No login, scrolling, search-result enumeration, cursor simulation, or apply
action is implemented.  A user may either import a reviewed JSON file or queue
one job URL for the existing read-only browser task, then explicitly ingest its
completed text into the normal `applications` pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.discovery.linkedin_discovery_v1 import (
    LinkedInDiscoveryError,
    MAX_AUTONOMOUS_DISCOVERY_RESULTS, MAX_DISCOVERY_RESULTS,
    linkedin_job_id,
    validate_job_url,
    validate_search_request,
    validate_saved_request,
)
from services.common.config import database_dsn
from services.intake.posting_identity import build_posting_identity
from services.intake.source_observation import find_and_observe_existing, observe_existing_posting

class IntakeError(ValueError):
    pass


def feature_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


def validate_linkedin_url(url: str) -> str:
    """Accept only one user-provided HTTPS LinkedIn job-detail URL.

    This is the intake boundary: search pages, profile pages, and arbitrary
    domains cannot be turned into browser work by this command.
    """
    try:
        return validate_job_url(url)
    except LinkedInDiscoveryError as exc:
        raise IntakeError(str(exc)) from exc


def extract_text(value: Any) -> str:
    """Best-effort unwrap of the existing browser worker's JSON result."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "raw_output", "finalAssistantVisibleText", "finalAssistantRawText"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        for key in ("parsed", "agent_response", "result", "payloads", "meta"):
            text = extract_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = extract_text(item)
            if text:
                return text
    return ""


def intake(cur, *, company: str, title: str, url: str, jd_text: str,
           location: str = "", work_mode: str = "", source: str) -> str | None:
    """Create or observe one user-reviewed LinkedIn posting.

    Source refreshes may update an application only while it remains at intake;
    downstream evidence snapshots are immutable and receive append-only source
    revisions instead.
    """
    url = validate_linkedin_url(url)
    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        raise IntakeError("company and title are required for every intake record.")
    jd_text = (jd_text or "").strip()
    if len(jd_text) < 200:
        raise IntakeError("JD text is too short; paste/review the full description first.")
    source_job_id = linkedin_job_id(url)
    identity = build_posting_identity(
        company=company, job_title=title, jd_text=jd_text, job_url=url,
        ats_hint="linkedin_browser_linked_session",
    )
    existing, _observation = find_and_observe_existing(
        cur, identity=identity, source_job_id=source_job_id, source_name="linkedin",
        company=company, job_title=title, jd_text=jd_text, location=location, work_mode=work_mode,
        metadata={"intake_channel": source},
    )
    if existing:
        return None
    cur.execute(
        """
        INSERT INTO applications
          (source, company, job_title, job_url, jd_text, jd_hash, current_step,
           status, intake_channel, ats_type, location, work_mode, source_job_id,
           first_seen_at, last_seen_at, created_at, updated_at)
        VALUES ('linkedin', %s, %s, %s, %s, %s, 'intake', 'active', %s,
                'linkedin_browser_linked_session', %s, %s, %s, now(), now(), now(), now())
        RETURNING id::text;
        """,
        (company, title, identity.canonical_url, jd_text, identity.jd_hash, source, location, work_mode, source_job_id),
    )
    app_id = cur.fetchone()[0]
    observe_existing_posting(
        cur, application_id=app_id, source_name="linkedin", source_job_id=source_job_id,
        company=company, job_title=title, job_url=identity.canonical_url,
        jd_text=jd_text, jd_hash=identity.jd_hash, location=location, work_mode=work_mode,
        metadata={"intake_channel": source, "initial": True},
    )
    cur.execute(
        """INSERT INTO pipeline_events
              (application_id, from_step, to_step, actor, reason, detail_json)
           VALUES (%s, NULL, 'intake', 'linkedin_intake', %s, %s);""",
        (app_id, "User-initiated LinkedIn job intake.", Jsonb({"source": source, "url": url})),
    )
    return app_id


def cmd_queue(cur, args) -> int:
    """Queue a read-only capture for one validated user-pasted job URL."""
    if not feature_enabled("JOBOS_LINKEDIN_READONLY_CAPTURE_ENABLED"):
        raise IntakeError("LinkedIn deterministic read-only capture is disabled by configuration.")
    url = validate_linkedin_url(args.url)
    cur.execute(
        """INSERT INTO browser_tasks
              (task_type, requested_by, status, priority, input_json, timeout_seconds)
           VALUES ('fetch_job_description', 'linkedin_intake_v1', 'queued', 'normal', %s, %s)
           RETURNING id::text;""",
        (Jsonb({"url": url, "user_initiated": True, "source": "linkedin",
                "deterministic_read_only": True}), args.timeout),
    )
    print(json.dumps({"browser_task_id": cur.fetchone()[0], "url": url,
                      "next": "Run browser_queue_worker, then linkedin_intake_v1.py ingest-task."}, indent=2))
    return 0



def _lock_idempotency(cur, key: str | None) -> None:
    if key:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (key,))


def safe_discovery_reissue(error_message: object) -> bool:
    """Return true only for failures that can safely recover without config repair.

    LinkedIn session expiry is retryable after a human logs back in.  OpenClaw
    gateway credentials/policy/configuration failures are *not* the same thing
    and must not be churned by the autonomous planner.
    """
    text = str(error_message or "").casefold()
    permanent_markers = (
        "openclaw auth failure", "token mismatch", "unauthorized",
        "unknown agent", "policy block", "configuration error", "permanenttaskerror",
    )
    if any(marker in text for marker in permanent_markers):
        return False
    return any(marker in text for marker in (
        "manual re-authentication", "login required", "authwall", "not signed in",
        "linkedin session", "timeout", "temporar", "connection", "rate limit",
        "transient", "session collision",
    ))


def queue_discovery_task(
    cur, *, request: dict[str, Any], timeout: int = 300,
    requested_by: str = "linkedin_intake_v1", autonomous: bool = False,
    idempotency_key: str | None = None,
) -> tuple[str, bool]:
    """Queue one validated LinkedIn discovery task with durable deduplication.

    ``autonomous`` is permitted only for the profile discovery planner and is
    represented explicitly in the task payload; manual callers retain
    ``user_initiated=true``.  A per-occurrence idempotency key plus durable rolling cooldown lets the
    periodic planner rerun the same search later without queue storms.
    """
    if not feature_enabled("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED"):
        raise IntakeError("LinkedIn autonomous discovery is disabled by configuration.")
    request = validate_search_request(
        str(request.get("keywords") or ""), str(request.get("location") or ""),
        int(request.get("max_results") or 0),
        max_allowed=(MAX_AUTONOMOUS_DISCOVERY_RESULTS if autonomous else MAX_DISCOVERY_RESULTS),
        date_posted=request.get("date_posted"),
        experience_levels=request.get("experience_levels"), employment_types=request.get("employment_types"),
        work_modes=request.get("work_modes"), companies=request.get("companies"), sort_by=request.get("sort_by"),
    )
    key = str(idempotency_key or "").strip() or None
    _lock_idempotency(cur, key)
    if key:
        cur.execute("SELECT id::text,status,error_message FROM browser_tasks WHERE idempotency_key=%s FOR UPDATE;", (key,))
        row = cur.fetchone()
        if row:
            # Discovery is read-only. A terminal transient/auth failure can be
            # safely reissued under the same durable request identity after the
            # user restores login, rather than waiting for a bucket boundary.
            if row[1] == "failed" and (not autonomous or safe_discovery_reissue(row[2])):
                cur.execute(
                    """UPDATE browser_tasks SET status='queued',error_message=NULL,
                              locked_by=NULL,lease_expires_at=NULL,finished_at=NULL,
                              retry_count=0,updated_at=now() WHERE id=%s;""",
                    (row[0],),
                )
                return str(row[0]), True
            return str(row[0]), False
    payload = {
        **request,
        "user_initiated": not autonomous,
        "autonomous_discovery": bool(autonomous),
        "source": "linkedin",
        "auto_ingest": True,
        "apply_search_preferences": bool(autonomous),
    }
    cur.execute(
        """INSERT INTO browser_tasks
              (task_type, requested_by, status, priority, input_json, timeout_seconds, idempotency_key)
           VALUES ('discover_linkedin_jobs', %s, 'queued', 'normal', %s, %s, %s)
           RETURNING id::text;""",
        (requested_by, Jsonb(payload), int(timeout), key),
    )
    return str(cur.fetchone()[0]), True


def queue_saved_sync_task(
    cur, *, max_results: int = 10, timeout: int = 600,
    requested_by: str = "linkedin_intake_v1", autonomous: bool = False,
    idempotency_key: str | None = None,
) -> tuple[str, str, bool]:
    """Queue one bounded Saved Jobs sync with the same periodic dedupe contract."""
    if not feature_enabled("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED"):
        raise IntakeError("LinkedIn Saved Jobs discovery is disabled by configuration.")
    request = validate_saved_request(int(max_results))
    key = str(idempotency_key or "").strip() or None
    _lock_idempotency(cur, key)
    if key:
        cur.execute(
            """SELECT bt.id::text, coalesce(bt.input_json->>'saved_sync_id',''), bt.status,bt.error_message
                 FROM browser_tasks bt WHERE bt.idempotency_key=%s;""",
            (key,),
        )
        row = cur.fetchone()
        if row:
            if row[2] == "failed" and (not autonomous or safe_discovery_reissue(row[3])):
                cur.execute(
                    """UPDATE browser_tasks SET status='queued',error_message=NULL,
                              locked_by=NULL,lease_expires_at=NULL,finished_at=NULL,
                              retry_count=0,updated_at=now() WHERE id=%s;""",
                    (row[0],),
                )
                cur.execute("UPDATE linkedin_saved_syncs SET status='queued',error_message=NULL,completed_at=NULL WHERE id=%s;", (row[1],))
                return str(row[1]), str(row[0]), True
            return str(row[1]), str(row[0]), False
    cur.execute(
        """INSERT INTO linkedin_saved_syncs(requested_limit, status)
           VALUES (%s, 'queued') RETURNING id::text;""",
        (request["max_results"],),
    )
    sync_id = str(cur.fetchone()[0])
    cur.execute(
        """INSERT INTO browser_tasks(
               task_type, requested_by, status, priority, input_json, timeout_seconds, idempotency_key)
           VALUES ('discover_linkedin_saved_jobs', %s, 'queued', 'normal', %s, %s, %s)
           RETURNING id::text;""",
        (requested_by, Jsonb({
            "max_results": request["max_results"],
            "user_initiated": not autonomous,
            "autonomous_discovery": bool(autonomous),
            "source": "linkedin", "auto_ingest": True, "saved_sync_id": sync_id,
        }), int(timeout), key),
    )
    task_id = str(cur.fetchone()[0])
    cur.execute("UPDATE linkedin_saved_syncs SET browser_task_id=%s WHERE id=%s;", (task_id, sync_id))
    return sync_id, task_id, True


def cmd_queue_discovery(cur, args) -> int:
    """Queue a bounded, user-requested search for the JobOS browser executor."""
    request = validate_search_request(
        args.keywords, args.location, args.max_results, date_posted=args.date_posted,
        experience_levels=args.experience_level, employment_types=args.employment_type,
        work_modes=args.work_mode_filter, companies=args.company, sort_by=args.sort_by,
    )
    task_id, created = queue_discovery_task(
        cur, request=request, timeout=args.timeout, requested_by="linkedin_intake_v1",
        autonomous=False,
    )
    print(json.dumps({
        "browser_task_id": task_id, "search": request, "created": created,
        "next": "Run the JobOS browser worker; validated JDs are auto-ingested into applications."
    }, indent=2))
    return 0


def cmd_queue_saved(cur, args) -> int:
    """Queue a bounded, read-only sync of jobs already saved by the user."""
    request = validate_saved_request(args.max_results)
    sync_id, task_id, created = queue_saved_sync_task(
        cur, max_results=request["max_results"], timeout=args.timeout,
        requested_by="linkedin_intake_v1", autonomous=False,
    )
    print(json.dumps({"saved_sync_id": sync_id, "browser_task_id": task_id,
                      "max_results": request["max_results"], "created": created,
                      "next": "Run the JobOS browser worker; saved jobs are auto-ingested read-only."}, indent=2))
    return 0


def cmd_ingest_task(cur, args) -> int:
    """Move a completed capture into applications after its text is reviewed."""
    cur.execute("SELECT status, input_json, result_json, error_message FROM browser_tasks WHERE id = %s;", (args.task_id,))
    row = cur.fetchone()
    if not row:
        raise IntakeError("Browser task not found.")
    if row[0] != "completed":
        raise IntakeError(f"Browser task is {row[0]!r}: {row[3] or 'not completed yet'}")
    payload = row[1] if isinstance(row[1], dict) else {}
    # Browser result envelopes have changed shape across workers/releases.  The
    # recursive extractor already understands text/parsed/result/payloads/meta,
    # so feed it the entire result instead of assuming an ``agent_response``
    # object exists at one exact level.
    jd_text = extract_text(row[2] or {})
    if "login" in jd_text.lower() and len(jd_text) < 500:
        raise IntakeError("Browser reported a login wall; paste an exported/reviewed JD instead.")
    app_id = intake(cur, company=args.company, title=args.title,
                    url=payload.get("url", ""), jd_text=jd_text,
                    location=args.location, work_mode=args.work_mode,
                    source="linkedin_browser_user_initiated")
    print(json.dumps({"application_id": app_id, "duplicate": app_id is None}, indent=2))
    return 0


def cmd_import(cur, args) -> int:
    """Preview or commit a user-reviewed LinkedIn JSON export."""
    if not feature_enabled("JOBOS_LINKEDIN_MANUAL_INTAKE_ENABLED"):
        raise IntakeError("LinkedIn manual/search-assisted intake is disabled by configuration.")
    try:
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntakeError(f"Import file is not valid JSON: {exc.msg}.") from exc
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = raw.get("jobs", [])
    else:
        raise IntakeError("Import file must be a JSON array or {\"jobs\": [...]} object.")
    if not isinstance(records, list):
        raise IntakeError("Import file must contain a jobs array.")
    created = duplicates = invalid = 0
    warnings: list[str] = []
    for position, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            invalid += 1
            warnings.append(f"record {position}: expected an object")
            continue
        try:
            app_id = intake(cur, company=item.get("company", ""), title=item.get("title", ""),
                            url=item.get("url") or item.get("job_url", ""),
                            jd_text=item.get("jd_text") or item.get("description", ""),
                            location=item.get("location", ""), work_mode=item.get("work_mode", ""),
                            source="linkedin_export_user_reviewed")
        except (IntakeError, LinkedInDiscoveryError, TypeError) as exc:
            # One incomplete reviewed record should not discard every other
            # valid record in the same import.  Keep the safety boundary (the
            # invalid record is never stored), report it, and continue.
            invalid += 1
            warnings.append(f"record {position}: {exc}")
            continue
        if app_id:
            created += 1
        else:
            duplicates += 1
    print(json.dumps({"created": created, "duplicates": duplicates, "invalid": invalid,
                      "warnings": warnings, "apply": args.apply}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="User-initiated LinkedIn job intake")
    subs = parser.add_subparsers(dest="command", required=True)
    queue = subs.add_parser("queue-fetch", help="Queue a single, user-pasted LinkedIn job URL for read-only capture.")
    queue.add_argument("--url", required=True)
    queue.add_argument("--timeout", type=int, default=180)
    discovery = subs.add_parser(
        "queue-discovery",
        help="Queue a bounded LinkedIn search for the linked JobOS browser profile."
    )
    discovery.add_argument("--keywords", required=True)
    discovery.add_argument("--location", default="")
    discovery.add_argument("--max-results", type=int, default=3)
    discovery.add_argument("--date-posted", choices=("24h", "week", "month"))
    discovery.add_argument("--experience-level", action="append", default=[])
    discovery.add_argument("--employment-type", action="append", default=[])
    discovery.add_argument("--work-mode-filter", action="append", default=[])
    discovery.add_argument("--company", action="append", default=[])
    discovery.add_argument("--sort-by", choices=("recent", "relevant"), default="recent")
    discovery.add_argument("--timeout", type=int, default=300)
    saved = subs.add_parser("queue-saved", help="Queue a bounded read-only sync of LinkedIn Saved Jobs.")
    saved.add_argument("--max-results", type=int, default=10)
    saved.add_argument("--timeout", type=int, default=600)
    completed = subs.add_parser("ingest-task", help="Ingest one completed capture task into applications.")
    completed.add_argument("--task-id", required=True)
    completed.add_argument("--company", required=True)
    completed.add_argument("--title", required=True)
    completed.add_argument("--location", default="")
    completed.add_argument("--work-mode", default="")
    imported = subs.add_parser("import", help="Import a user-reviewed LinkedIn JSON export.")
    imported.add_argument("--file", required=True)
    imported.add_argument("--apply", action="store_true", help="Commit; otherwise run in a rolled-back transaction.")
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                code = {
                    "queue-fetch": cmd_queue, "queue-discovery": cmd_queue_discovery,
                    "queue-saved": cmd_queue_saved,
                    "ingest-task": cmd_ingest_task, "import": cmd_import,
                }[args.command](cur, args)
            if getattr(args, "apply", False) or args.command in {"queue-fetch", "queue-discovery", "queue-saved", "ingest-task"}:
                conn.commit()
            else:
                conn.rollback()
                print("DRY RUN. Nothing was committed; re-run import with --apply.")
            return code
        except (IntakeError, LinkedInDiscoveryError, KeyError, TypeError) as exc:
            conn.rollback()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
