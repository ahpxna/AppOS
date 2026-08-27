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
    linkedin_job_id,
    validate_job_url,
    validate_search_request,
    validate_saved_request,
)
from services.common.config import database_dsn
from services.intake.posting_identity import build_posting_identity, find_existing_application
from services.intake.source_observation import observe_existing_posting

DSN = database_dsn()


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
    existing = find_existing_application(cur, identity, source_job_id=source_job_id)
    if existing:
        observe_existing_posting(
            cur, application_id=existing[0], source_name="linkedin", source_job_id=source_job_id,
            company=company, job_title=title, job_url=identity.canonical_url,
            jd_text=jd_text, jd_hash=identity.jd_hash, location=location, work_mode=work_mode,
            metadata={"intake_channel": source},
        )
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


def cmd_queue_discovery(cur, args) -> int:
    """Queue a bounded, user-requested search for the JobOS browser executor."""
    if not feature_enabled("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED"):
        raise IntakeError("LinkedIn autonomous discovery is disabled by configuration.")
    request = validate_search_request(
        args.keywords, args.location, args.max_results, date_posted=args.date_posted,
        experience_levels=args.experience_level, employment_types=args.employment_type,
        work_modes=args.work_mode_filter, companies=args.company, sort_by=args.sort_by,
    )
    cur.execute(
        """INSERT INTO browser_tasks
              (task_type, requested_by, status, priority, input_json, timeout_seconds)
           VALUES ('discover_linkedin_jobs', 'linkedin_intake_v1', 'queued', 'normal', %s, %s)
           RETURNING id::text;""",
        (Jsonb({**request, "user_initiated": True, "source": "linkedin",
                "auto_ingest": True}), args.timeout),
    )
    print(json.dumps({
        "browser_task_id": cur.fetchone()[0], "search": request,
        "next": "Run the JobOS browser worker; validated JDs are auto-ingested into applications."
    }, indent=2))
    return 0


def cmd_queue_saved(cur, args) -> int:
    """Queue a bounded, read-only sync of jobs already saved by the user."""
    if not feature_enabled("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED"):
        raise IntakeError("LinkedIn Saved Jobs discovery is disabled by configuration.")
    request = validate_saved_request(args.max_results)
    cur.execute(
        """INSERT INTO linkedin_saved_syncs(requested_limit, status)
           VALUES (%s, 'queued') RETURNING id::text;""",
        (request["max_results"],),
    )
    sync_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO browser_tasks(
               task_type, requested_by, status, priority, input_json, timeout_seconds)
           VALUES ('discover_linkedin_saved_jobs', 'linkedin_intake_v1', 'queued', 'normal', %s, %s)
           RETURNING id::text;""",
        (Jsonb({"max_results": request["max_results"], "user_initiated": True,
                "source": "linkedin", "auto_ingest": True, "saved_sync_id": sync_id}),
         args.timeout),
    )
    task_id = cur.fetchone()[0]
    cur.execute("UPDATE linkedin_saved_syncs SET browser_task_id = %s WHERE id = %s;", (task_id, sync_id))
    print(json.dumps({"saved_sync_id": sync_id, "browser_task_id": task_id,
                      "max_results": request["max_results"],
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
    payload = row[1] or {}
    result = row[2] or {}
    jd_text = extract_text(result.get("agent_response"))
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
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else raw.get("jobs", [])
    if not isinstance(records, list):
        raise IntakeError("Import file must be a JSON array or {\"jobs\": [...]}.")
    created = duplicates = 0
    for item in records:
        app_id = intake(cur, company=item.get("company", ""), title=item.get("title", ""),
                        url=item.get("url") or item.get("job_url", ""),
                        jd_text=item.get("jd_text") or item.get("description", ""),
                        location=item.get("location", ""), work_mode=item.get("work_mode", ""),
                        source="linkedin_export_user_reviewed")
        if app_id:
            created += 1
        else:
            duplicates += 1
    print(json.dumps({"created": created, "duplicates": duplicates, "apply": args.apply}, indent=2))
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
    with psycopg.connect(DSN, autocommit=False) as conn:
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
