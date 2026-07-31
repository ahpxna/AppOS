"""
L3 -- BROWSER QUEUE WORKER (OpenClaw CLI transport)

Bridges browser_tasks to a local OpenClaw gateway via `openclaw agent`.

Division of responsibility:
  L6 decides WHAT text exists. Every claim it produces is verified against
  approved profile assets by the truth checker.
  L3 only CARRIES verified text to a page. This worker refuses to type any
  content that did not come from a generated_document with qa_status='pass'
  AND approved=true, so the browser layer cannot become a second, ungrounded
  writing path that bypasses the approval gate.

Schema notes (these bite):
  browser_tasks.error_message      <- errors on the live task
  dead_letter_tasks.last_error     <- errors on the archived task
  browser_tasks.result_json        <- NOT output_json

Failure policy:
  Deterministic failures (bad task type, missing approval, unverified document)
  go straight to 'failed'. Retrying them changes nothing and would spin forever.
  Transient failures (timeout, gateway down) increment retry_count and requeue
  until max_retries, then dead-letter.

Usage:
  python services/browser-controller/browser_queue_worker.py --health
  python services/browser-controller/browser_queue_worker.py --once
  python services/browser-controller/browser_queue_worker.py --poll-seconds 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from services.common.observability import emit_trace, make_trace_id

# ---------------------------------------------------------------- config

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")
# Agent ids as defined in ~/.openclaw/openclaw.json -> agents.list
OPENCLAW_AGENT_BROWSE = os.getenv("OPENCLAW_AGENT_BROWSE", "main")
OPENCLAW_AGENT_RESUME = os.getenv("OPENCLAW_AGENT_RESUME", "resume")
OPENCLAW_AGENT_COVER = os.getenv("OPENCLAW_AGENT_COVER", "cover_letter")

WORKER_VERSION = "browser_queue_worker_v2_openclaw_cli_2026_07_28"
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
LEASE_SECONDS = int(os.getenv("JOBOS_BROWSER_LEASE_SECONDS", "600"))

SUPPORTED_TASKS = {
    "fetch_job_description",
    "capture_page_snapshot",
    "fill_application_form",
}
REQUIRES_APPROVAL = {"fill_application_form"}


class PermanentTaskError(Exception):
    """Retrying will not help. Fail the task immediately."""


class TransientTaskError(Exception):
    """Might succeed later. Requeue until max_retries."""


# ---------------------------------------------------------------- openclaw CLI

def run_openclaw(args, *, timeout: int) -> subprocess.CompletedProcess:
    if shutil.which(OPENCLAW_BIN) is None:
        raise PermanentTaskError(
            f"'{OPENCLAW_BIN}' not found on PATH. "
            "Set OPENCLAW_BIN to the absolute path if it lives under nvm."
        )
    try:
        return subprocess.run(
            [OPENCLAW_BIN, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise TransientTaskError(f"openclaw timed out after {timeout}s") from e


def parse_agent_output(stdout: str) -> Dict[str, Any]:
    """`openclaw agent --json` prints banner text before the JSON payload.
    Scan lines from the bottom for the last parseable JSON object."""
    lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.startswith("{") or line.startswith("["):
            try:
                return {"parsed": json.loads(line)}
            except json.JSONDecodeError:
                continue
    # Some builds pretty-print the object across several lines.
    first, last = stdout.find("{"), stdout.rfind("}")
    if first != -1 and last > first:
        try:
            return {"parsed": json.loads(stdout[first:last + 1])}
        except json.JSONDecodeError:
            pass
    return {"raw_output": stdout.strip()[:8000]}


def openclaw_agent(*, agent: str, message: str, timeout: int,
                   session_id: Optional[str] = None) -> Dict[str, Any]:
    args = [
        "agent",
        "--agent", agent,
        "--message", message,
        "--json",
        "--timeout", str(timeout),
    ]
    if session_id:
        # Each task gets its own session. Sharing agent:main:global causes
        # "session changed while starting work" collisions when two calls
        # overlap, and lets one posting's context bleed into the next one's.
        args += ["--session-id", session_id]

    proc = run_openclaw(args, timeout=timeout + 30)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        low = err.lower()
        if "unauthorized" in low or "token mismatch" in low:
            raise PermanentTaskError(
                f"OpenClaw auth failure: {err}. "
                "gateway.remote.token must equal gateway.auth.token."
            )
        if "agent" in low and "not found" in low:
            raise PermanentTaskError(f"Unknown agent id '{agent}': {err}")
        if "session" in low and "changed while starting" in low:
            # Transient: another call held the session. Retrying with a fresh
            # session id normally clears it.
            raise TransientTaskError(f"session collision: {err}")
        raise TransientTaskError(f"openclaw agent exit {proc.returncode}: {err}")

    return parse_agent_output(proc.stdout)


def openclaw_health():
    """`gateway status` prints 'Runtime: running' even when RPC is broken,
    so probe the RPC path itself."""
    proc = run_openclaw(["health"], timeout=30)
    out = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0 and "unauthorized" not in out.lower()
    return ok, out[:1500]


# ---------------------------------------------------------------- queue

def reap_expired_leases(cur) -> int:
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'queued', locked_by = NULL, lease_expires_at = NULL,
            retry_count = retry_count + 1
        WHERE status = 'running'
          AND lease_expires_at < now()
          AND retry_count < max_retries
        RETURNING id;
        """
    )
    return len(cur.fetchall())


def dead_letter_exhausted(cur) -> int:
    cur.execute(
        """
        WITH dead AS (
          UPDATE browser_tasks
          SET status = 'failed', locked_by = NULL, lease_expires_at = NULL,
              finished_at = now(),
              error_message = COALESCE(error_message, 'Max retries exceeded')
          WHERE status = 'running'
            AND lease_expires_at < now()
            AND retry_count >= max_retries
          RETURNING id, task_type, application_id, input_json,
                    error_message, retry_count, screenshot_url
        )
        INSERT INTO dead_letter_tasks (
          original_task_id, task_type, application_id,
          input_json, last_error, retry_count, screenshot_url, created_at
        )
        SELECT id, task_type, application_id, input_json,
               error_message, retry_count, screenshot_url, now()
        FROM dead
        RETURNING id;
        """
    )
    return len(cur.fetchall())


def claim_next_task(cur) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'running', locked_by = %s,
            lease_expires_at = now() + make_interval(secs => %s),
            started_at = COALESCE(started_at, now())
        WHERE id = (
          SELECT id FROM browser_tasks
          WHERE status = 'queued'
          ORDER BY
            CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
            created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        RETURNING id::text, task_type, application_id::text,
                  input_json, timeout_seconds, retry_count, max_retries;
        """,
        (WORKER_ID, LEASE_SECONDS),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "task_type": row[1], "application_id": row[2],
        "input_json": row[3] or {}, "timeout_seconds": row[4] or 300,
        "retry_count": row[5] or 0,
        "max_retries": row[6] if row[6] is not None else 2,
    }


def complete_task(cur, task_id: str, result: Dict[str, Any]) -> None:
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'completed', result_json = %s, error_message = NULL,
            locked_by = NULL, lease_expires_at = NULL, finished_at = now()
        WHERE id = %s;
        """,
        (Jsonb(result), task_id),
    )


def fail_task(cur, task_id: str, error: str) -> None:
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'failed', error_message = %s,
            locked_by = NULL, lease_expires_at = NULL, finished_at = now()
        WHERE id = %s;
        """,
        (error[:2000], task_id),
    )


def requeue_or_fail(cur, task: Dict[str, Any], error: str) -> str:
    new_count = task["retry_count"] + 1
    exhausted = new_count > task["max_retries"]
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = %s, retry_count = %s, error_message = %s,
            locked_by = NULL, lease_expires_at = NULL,
            finished_at = CASE WHEN %s THEN now() ELSE NULL END
        WHERE id = %s;
        """,
        ("failed" if exhausted else "queued", new_count, error[:2000],
         exhausted, task["id"]),
    )
    return "failed" if exhausted else "queued"


# ---------------------------------------------------------------- guards

def require_verified_document(cur, document_id: str) -> Dict[str, Any]:
    """The single chokepoint that keeps L3 from writing ungrounded text."""
    cur.execute(
        """
        SELECT id::text, doc_type, content, qa_status, approved
        FROM generated_documents
        WHERE id = %s;
        """,
        (document_id,),
    )
    row = cur.fetchone()
    if not row:
        raise PermanentTaskError(f"generated_document not found: {document_id}")

    doc = {"id": row[0], "doc_type": row[1], "content": row[2],
           "qa_status": row[3], "approved": row[4]}

    if doc["qa_status"] != "pass":
        raise PermanentTaskError(
            f"Document {document_id} has qa_status={doc['qa_status']!r}. "
            "Only text that passed the truth checker may be typed into a form."
        )
    if not doc["approved"]:
        raise PermanentTaskError(
            f"Document {document_id} is not approved by the user yet."
        )
    if not (doc["content"] or "").strip():
        raise PermanentTaskError(f"Document {document_id} has empty content.")
    return doc


def require_approval(cur, application_id: Optional[str]) -> None:
    if not application_id:
        raise PermanentTaskError(
            "Task touches a real application form but has no application_id."
        )
    cur.execute(
        """
        SELECT 1 FROM approval_requests
        WHERE application_id = %s
          AND status = 'approved'
          AND token_expires_at > now()
        LIMIT 1;
        """,
        (application_id,),
    )
    if cur.fetchone() is None:
        raise PermanentTaskError(
            "No valid, unexpired approval_request for this application. "
            "Human approval is required before touching a real form."
        )


def load_allowed_domains(cur) -> list:
    cur.execute("SELECT domain FROM allowed_domains WHERE enabled = true;")
    return [r[0].lower() for r in cur.fetchall()]


def check_domain(cur, url: str) -> None:
    """The domain whitelist existed in the schema (allowed_domains,
    migration 038) and was enforced inside autofill_agent_v1.py, but this
    worker -- the actual single chokepoint where every browser task
    (including ones NOT run through autofill_agent_v1.py) reaches
    OpenClaw -- never called it. A JD or company page containing a link
    could send the agent anywhere. Fixed 2026-07-31: enforced here too,
    same logic as autofill_agent_v1.py's check_domain."""
    m = re.search(r"https?://([^/]+)", url)
    host = (m.group(1) if m else "").lower().split(":")[0]
    for domain in load_allowed_domains(cur):
        if host == domain or host.endswith("." + domain):
            return
    raise PermanentTaskError(
        f"Domain '{host}' is not in allowed_domains. Add it deliberately:\n"
        f"  INSERT INTO allowed_domains (domain, category) "
        f"VALUES ('{host}', 'ats');"
    )


def require_url(cur, input_json: Dict[str, Any]) -> str:
    url = (input_json.get("url") or "").strip()
    if not url:
        raise PermanentTaskError("input_json.url is required.")
    if not url.startswith(("http://", "https://")):
        raise PermanentTaskError(f"Refusing non-http(s) URL: {url[:120]}")
    check_domain(cur, url)
    return url


# ---------------------------------------------------------------- handlers

def handle_fetch_job_description(cur, task) -> Dict[str, Any]:
    url = require_url(cur, task["input_json"])
    msg = (
        "Open this URL and return the full job description text.\n"
        f"URL: {url}\n\n"
        "Return the posting text only. Do not summarise or comment. "
        "Do not fill in, click, or submit anything. "
        "If the page requires login, say so instead of authenticating."
    )
    return {
        "url": url,
        "agent_response": openclaw_agent(
            agent=OPENCLAW_AGENT_BROWSE, message=msg,
            timeout=task["timeout_seconds"],
            session_id=f"jobos-task-{task['id']}", 
        ),
    }


def handle_capture_page_snapshot(cur, task) -> Dict[str, Any]:
    url = require_url(cur, task["input_json"])
    msg = (
        "Open this URL and list every form field: label, input type, whether it "
        "is required, and any select options.\n"
        f"URL: {url}\n\n"
        "This is a read-only inspection. Do not type into any field. "
        "Do not submit."
    )
    return {
        "url": url,
        "agent_response": openclaw_agent(
            agent=OPENCLAW_AGENT_BROWSE, message=msg,
            timeout=task["timeout_seconds"],
            session_id=f"jobos-task-{task['id']}",
        ),
    }


def handle_fill_application_form(cur, task) -> Dict[str, Any]:
    inp = task["input_json"]
    url = require_url(cur, inp)
    document_id = inp.get("generated_document_id")
    if not document_id:
        raise PermanentTaskError(
            "fill_application_form requires input_json.generated_document_id. "
            "Raw text is deliberately not accepted here."
        )

    doc = require_verified_document(cur, document_id)

    agent = {
        "resume": OPENCLAW_AGENT_RESUME,
        "cover_letter": OPENCLAW_AGENT_COVER,
    }.get(doc["doc_type"], OPENCLAW_AGENT_BROWSE)

    msg = (
        "Fill this application form using ONLY the text between the markers "
        "below.\n"
        f"URL: {url}\n\n"
        "Rules:\n"
        "1. Use the provided text as-is. Do not rewrite, expand, embellish, or "
        "invent answers for fields the text does not cover.\n"
        "2. Leave any field you cannot fill from the text EMPTY, and report it "
        "as unfilled.\n"
        "3. DO NOT CLICK SUBMIT. Stop once the fields are populated.\n"
        "4. Take a screenshot of the filled form and return its path.\n\n"
        f"--- BEGIN APPROVED {doc['doc_type'].upper()} ---\n"
        f"{doc['content']}\n"
        f"--- END APPROVED {doc['doc_type'].upper()} ---"
    )

    resp = openclaw_agent(
        agent=agent, message=msg, timeout=task["timeout_seconds"], session_id=f"jobos-task-{task['id']}"
    )
    return {
        "url": url,
        "generated_document_id": document_id,
        "doc_type": doc["doc_type"],
        "submitted": False,
        "note": "Fields populated. Final submit is a human action by design.",
        "agent_response": resp,
    }


HANDLERS = {
    "fetch_job_description": handle_fetch_job_description,
    "capture_page_snapshot": handle_capture_page_snapshot,
    "fill_application_form": handle_fill_application_form,
}


# ---------------------------------------------------------------- loop

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    print("\nShutdown requested; finishing current task first.")


def process_one(conn) -> bool:
    with conn.cursor() as cur:
        reaped = reap_expired_leases(cur)
        dead = dead_letter_exhausted(cur)
        conn.commit()
        if reaped:
            print(f"Reaped {reaped} expired lease(s).")
        if dead:
            print(f"Dead-lettered {dead} exhausted task(s).")

        task = claim_next_task(cur)
        conn.commit()
        if not task:
            return False

    print(f"\n[{task['task_type']}] {task['id']} "
          f"(retry {task['retry_count']}/{task['max_retries']})")
    trace_id = make_trace_id("browser-task", task["id"])
    start = time.perf_counter()

    try:
        with conn.cursor() as cur:
            if task["task_type"] not in SUPPORTED_TASKS:
                raise PermanentTaskError(
                    f"Unsupported task_type: {task['task_type']}"
                )
            if task["task_type"] in REQUIRES_APPROVAL:
                require_approval(cur, task["application_id"])

            start = time.perf_counter()
            result = HANDLERS[task["task_type"]](cur, task)
            elapsed = time.perf_counter() - start

            result["worker_version"] = WORKER_VERSION
            result["elapsed_seconds"] = round(elapsed, 1)
            complete_task(cur, task["id"], result)
        conn.commit()
        print(f"  completed in {elapsed:.1f}s")
        emit_trace(
            trace_id,
            "browser_task",
            started_at=start,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            task_id=task["id"],
            task_type=task["task_type"],
            status="completed",
        )

    except PermanentTaskError as e:
        conn.rollback()
        with conn.cursor() as cur:
            fail_task(cur, task["id"], str(e))
        conn.commit()
        print(f"  refused (no retry): {e}")
        emit_trace(
            trace_id,
            "browser_task",
            started_at=start,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            task_id=task["id"],
            task_type=task["task_type"],
            status="failed",
        )

    except TransientTaskError as e:
        conn.rollback()
        with conn.cursor() as cur:
            status = requeue_or_fail(cur, task, str(e))
        conn.commit()
        print(f"  transient error -> {status}: {e}")
        emit_trace(
            trace_id,
            "browser_task",
            started_at=start,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            task_id=task["id"],
            task_type=task["task_type"],
            status=status,
        )

    except Exception as e:
        conn.rollback()
        with conn.cursor() as cur:
            status = requeue_or_fail(cur, task, f"{type(e).__name__}: {e}")
        conn.commit()
        print(f"  unexpected error -> {status}: {type(e).__name__}: {e}")
        emit_trace(
            trace_id,
            "browser_task",
            started_at=start,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            task_id=task["id"],
            task_type=task["task_type"],
            status=status,
        )

    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-seconds", type=int, default=10)
    p.add_argument("--health", action="store_true")
    args = p.parse_args()

    if args.health:
        print(f"OpenClaw binary: {shutil.which(OPENCLAW_BIN) or 'NOT FOUND'}")
        try:
            ok, out = openclaw_health()
        except (PermanentTaskError, TransientTaskError) as e:
            print(f"FAIL: {e}")
            return 1
        print(out)
        print("\nRPC reachable" if ok else "\nRPC NOT reachable")
        return 0 if ok else 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print("===== BROWSER QUEUE WORKER =====")
    print(f"Worker:    {WORKER_VERSION}")
    print(f"Worker id: {WORKER_ID}")
    print(f"Transport: {OPENCLAW_BIN} agent --json")
    print("Concurrency: 1 task at a time (single browser session)\n")

    with psycopg.connect(DSN, autocommit=False) as conn:
        if args.once:
            if not process_one(conn):
                print("Queue empty.")
            return 0
        while not _shutdown:
            if not process_one(conn):
                time.sleep(args.poll_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
