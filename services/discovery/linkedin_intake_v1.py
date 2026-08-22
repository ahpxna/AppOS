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
    validate_search_request,
)

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")
DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"


class IntakeError(ValueError):
    pass


def validate_linkedin_url(url: str) -> str:
    """Accept only one user-provided HTTPS LinkedIn job-detail URL.

    This is the intake boundary: search pages, profile pages, and arbitrary
    domains cannot be turned into browser work by this command.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or not (parsed.hostname or "").lower().endswith("linkedin.com"):
        raise IntakeError("URL must be an https LinkedIn URL.")
    if not parsed.path.startswith("/jobs/"):
        raise IntakeError("URL must point to an individual LinkedIn job page under /jobs/.")
    return parsed.geturl()


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
    """Create one deduplicated application from user-reviewed JD text."""
    url = validate_linkedin_url(url)
    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        raise IntakeError("company and title are required for every intake record.")
    jd_text = (jd_text or "").strip()
    if len(jd_text) < 200:
        raise IntakeError("JD text is too short; paste/review the full description first.")
    jd_hash = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
    cur.execute("SELECT id::text FROM applications WHERE jd_hash = %s;", (jd_hash,))
    if cur.fetchone():
        return None
    cur.execute(
        """
        INSERT INTO applications
          (source, company, job_title, job_url, jd_text, jd_hash, current_step,
           status, intake_channel, ats_type, location, work_mode, created_at, updated_at)
        VALUES ('linkedin', %s, %s, %s, %s, %s, 'intake', 'active', %s,
                'linkedin_user_initiated', %s, %s, now(), now())
        RETURNING id::text;
        """,
        (company, title, url, jd_text, jd_hash, source, location, work_mode),
    )
    app_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO pipeline_events
              (application_id, from_step, to_step, actor, reason, detail_json)
           VALUES (%s, NULL, 'intake', 'linkedin_intake', %s, %s);""",
        (app_id, "User-initiated LinkedIn job intake.", Jsonb({"source": source, "url": url})),
    )
    return app_id


def cmd_queue(cur, args) -> int:
    """Queue a read-only capture for one validated user-pasted job URL."""
    url = validate_linkedin_url(args.url)
    cur.execute(
        """INSERT INTO browser_tasks
              (task_type, requested_by, status, priority, input_json, timeout_seconds)
           VALUES ('fetch_job_description', 'linkedin_intake_v1', 'queued', 'normal', %s, %s)
           RETURNING id::text;""",
        (Jsonb({"url": url, "user_initiated": True, "source": "linkedin"}), args.timeout),
    )
    print(json.dumps({"browser_task_id": cur.fetchone()[0], "url": url,
                      "next": "Run browser_queue_worker, then linkedin_intake_v1.py ingest-task."}, indent=2))
    return 0


def cmd_queue_discovery(cur, args) -> int:
    """Queue a bounded, user-requested search for the JobOS browser executor."""
    request = validate_search_request(args.keywords, args.location, args.max_results)
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
    discovery.add_argument("--timeout", type=int, default=300)
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
                    "ingest-task": cmd_ingest_task, "import": cmd_import,
                }[args.command](cur, args)
            if getattr(args, "apply", False) or args.command in {"queue-fetch", "queue-discovery", "ingest-task"}:
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
