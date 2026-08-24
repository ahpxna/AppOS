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
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg.types.json import Jsonb

from services.common.observability import emit_trace, make_trace_id
from services.common.config import database_dsn, load_repo_env
from services.discovery.linkedin_discovery_v1 import (
    LinkedInDiscoveryError,
    ingest_discovered_jobs,
    ingest_saved_jobs,
    validate_job_url,
    validate_search_request,
    validate_saved_request,
)
from services.autofill.autofill_agent_v1 import parse_snapshot
from services.autofill.autofill_executor_v1 import OpenClawTransport, TransportError
from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.autofill_session_v1 import AutofillSession, SessionError, SnapshotState
from services.autofill.form_inspector_v1 import inspect_nodes, inspect_question_groups
from services.common.autofill_identity import canonical_page_url, page_fingerprint
from services.autofill.autofill_context_v1 import AutofillContextError, load_autofill_context
from services.common.openclaw_runtime import resolve_openclaw_binary
from services.common.immigration_semantics import classify_immigration_question
from services.common.question_memory import normalize_question


load_repo_env()

# ---------------------------------------------------------------- config

DSN = database_dsn()

# Prefer the pinned JobOS runtime when setup installed it.  A global OpenClaw
# can otherwise inherit an unsupported Node version or a different plugin set.
OPENCLAW_BIN = resolve_openclaw_binary()
# Agent ids as defined in ~/.openclaw/openclaw.json -> agents.list
OPENCLAW_AGENT_BROWSE = os.getenv("OPENCLAW_AGENT_BROWSE", "main")
OPENCLAW_AGENT_LINKEDIN_DISCOVERY = (
    os.getenv("OPENCLAW_AGENT_LINKEDIN_DISCOVERY", "linkedin_discovery").strip()
    or "linkedin_discovery"
)
OPENCLAW_AGENT_RESUME = os.getenv("OPENCLAW_AGENT_RESUME", "resume")
OPENCLAW_AGENT_COVER = os.getenv("OPENCLAW_AGENT_COVER", "cover_letter")
# This is a host-side probe. For the Docker overlay, OpenClaw itself is
# configured with http://browser:9222 while the host worker uses the loopback
# port published by docker-compose.openclaw.yml.
BROWSER_CDP_URL = (
    os.getenv("JOBOS_BROWSER_CDP_URL")
    or os.getenv("OPENCLAW_BROWSER_CDP_URL")
    or "http://127.0.0.1:9222"
).rstrip("/")

WORKER_VERSION = "browser_queue_worker_v4_deterministic_form_session_2026_08_23"
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
LEASE_SECONDS = int(os.getenv("JOBOS_BROWSER_LEASE_SECONDS", "600"))

SUPPORTED_TASKS = {
    "fetch_job_description",
    "discover_linkedin_jobs",
    "discover_linkedin_saved_jobs",
    "capture_page_snapshot",
    "fill_application_form",
}
REQUIRES_APPROVAL = {"fill_application_form"}


def feature_enabled(name: str, default: bool = True) -> bool:
    """Read a deliberately explicit local feature switch without guessing."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


class PermanentTaskError(Exception):
    """Retrying will not help. Fail the task immediately."""


class TransientTaskError(Exception):
    """Might succeed later. Requeue until max_retries."""


# ---------------------------------------------------------------- openclaw CLI

def openclaw_runtime_env() -> Dict[str, str]:
    """Run the bundled CLI with its compatible Node runtime when present."""
    env = dict(os.environ)
    explicit = os.getenv("OPENCLAW_NODE_BIN", "").strip()
    candidates = [Path(explicit)] if explicit else sorted(
        (REPO_ROOT / "data" / "openclaw-runtime").glob("node-runtime-*/bin"), reverse=True
    )
    if candidates and (candidates[0] / "node").is_file():
        env["PATH"] = f"{candidates[0]}{os.pathsep}{env.get('PATH', '')}"
    return env


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
            env=openclaw_runtime_env(),
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


def browser_cdp_health() -> tuple[bool, str]:
    """Probe Chrome's standard CDP metadata endpoint without driving a tab.

    A successful gateway response alone is insufficient: the historical
    browser failure was a Chrome process with no CDP listener. This request
    neither invokes a model nor reads browser cookies, page content, or tabs.
    """
    url = BROWSER_CDP_URL + "/json/version"
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return False, f"CDP unavailable at {BROWSER_CDP_URL}: {exc}"
    browser = payload.get("Browser") if isinstance(payload, dict) else None
    websocket = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
    if not browser or not websocket:
        return False, f"CDP returned an incomplete version payload at {BROWSER_CDP_URL}."
    return True, f"CDP reachable at {BROWSER_CDP_URL} ({browser})"


# ---------------------------------------------------------------- queue

def reap_expired_leases(cur) -> int:
    # A crash before an action is journaled is provably pre-I/O. Release that
    # exact capability/task pair for a safe retry, but respect max_retries.
    # Once a journal row exists, an external write may have happened and the
    # task must be reconciled instead of replayed.
    cur.execute(
        """
        WITH exhausted AS (
          SELECT b.id AS task_id, b.approval_request_id
          FROM browser_tasks b
          JOIN approval_requests a ON a.id = b.approval_request_id
          WHERE b.status = 'running'
            AND b.lease_expires_at < now()
            AND b.execution_state = 'executing'
            AND b.retry_count >= b.max_retries
            AND a.status = 'executing'
            AND a.executing_task_id = b.id
            AND NOT EXISTS (
              SELECT 1 FROM autofill_action_journal j WHERE j.browser_task_id = b.id
            )
          FOR UPDATE OF b, a SKIP LOCKED
        ), closed AS (
          UPDATE approval_requests a
          SET status = 'expired', executing_task_id = NULL,
              action_note = COALESCE(action_note, 'Autofill task exhausted before external I/O.')
          FROM exhausted e
          WHERE a.id = e.approval_request_id
          RETURNING e.task_id
        )
        UPDATE browser_tasks b
        SET status = 'failed', execution_state = 'not_started',
            locked_by = NULL, lease_expires_at = NULL,
            retry_count = retry_count + 1, finished_at = now(),
            error_message = COALESCE(error_message, 'Max retries exceeded before browser I/O.')
        FROM closed c
        WHERE b.id = c.task_id
        RETURNING b.id;
        """
    )
    exhausted_pre_io_rows = cur.fetchall()
    exhausted_pre_io = len(exhausted_pre_io_rows)
    for (task_id,) in exhausted_pre_io_rows:
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'failed', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": "lease expired; retries exhausted before browser I/O"}), task_id),
        )

    cur.execute(
        """
        WITH safe AS (
          SELECT b.id AS task_id, b.approval_request_id
          FROM browser_tasks b
          JOIN approval_requests a ON a.id = b.approval_request_id
          WHERE b.status = 'running'
            AND b.lease_expires_at < now()
            AND b.execution_state = 'executing'
            AND b.retry_count < b.max_retries
            AND a.status = 'executing'
            AND a.executing_task_id = b.id
            AND NOT EXISTS (
              SELECT 1 FROM autofill_action_journal j WHERE j.browser_task_id = b.id
            )
          FOR UPDATE OF b, a SKIP LOCKED
        ), released AS (
          UPDATE approval_requests a
          SET status = 'approved', executing_task_id = NULL
          FROM safe s
          WHERE a.id = s.approval_request_id
          RETURNING s.task_id
        )
        UPDATE browser_tasks b
        SET status = 'queued', execution_state = 'not_started', locked_by = NULL,
            lease_expires_at = NULL, retry_count = retry_count + 1
        FROM released r
        WHERE b.id = r.task_id
        RETURNING b.id;
        """
    )
    pre_io_rows = cur.fetchall()
    pre_io = len(pre_io_rows)
    for (task_id,) in pre_io_rows:
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'failed', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": "worker lease expired before first browser action; safe retry"}), task_id),
        )

    # Once deterministic execution may have produced a side effect, never put
    # the task back on the queue after a worker crash.
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'failed', execution_state = 'needs_reconciliation',
            locked_by = NULL, lease_expires_at = NULL, finished_at = now(),
            error_message = COALESCE(error_message, 'Worker lease expired during browser execution; reconcile before retrying.')
        WHERE status = 'running'
          AND lease_expires_at < now()
          AND execution_state IN ('executing', 'partial', 'completed', 'needs_reconciliation')
        RETURNING id;
        """
    )
    unsafe_rows = cur.fetchall()
    unsafe = len(unsafe_rows)
    for (task_id,) in unsafe_rows:
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'needs_review', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": "worker lease expired after browser execution may have started"}), task_id),
        )

    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'queued', locked_by = NULL, lease_expires_at = NULL,
            retry_count = retry_count + 1
        WHERE status = 'running'
          AND lease_expires_at < now()
          AND execution_state = 'not_started'
          AND retry_count < max_retries
        RETURNING id;
        """
    )
    plain_pre_io = len(cur.fetchall())
    return exhausted_pre_io + pre_io + unsafe + plain_pre_io


def _expire_bound_pre_io_approval(cur, task: Dict[str, Any], reason: str) -> None:
    approval_id = task.get("approval_request_id")
    if not approval_id:
        return
    cur.execute(
        """
        UPDATE approval_requests
        SET status = 'expired', executing_task_id = NULL,
            action_note = COALESCE(action_note, %s)
        WHERE id = %s AND application_id = %s
          AND status IN ('pending', 'approved', 'executing')
          AND consumed_at IS NULL
          AND (executing_task_id IS NULL OR executing_task_id = %s);
        """,
        (reason[:500], approval_id, task.get("application_id"), task["id"]),
    )


def dead_letter_exhausted(cur) -> int:
    """Archive exhausted, provably pre-I/O tasks and close their capability."""
    cur.execute(
        """
        UPDATE approval_requests a
        SET status = 'expired', executing_task_id = NULL,
            action_note = COALESCE(action_note, 'Browser task exhausted before external I/O.')
        FROM browser_tasks b
        WHERE b.status = 'running'
          AND b.lease_expires_at < now()
          AND b.execution_state = 'not_started'
          AND b.retry_count >= b.max_retries
          AND a.id = b.approval_request_id
          AND a.status IN ('pending', 'approved')
          AND a.consumed_at IS NULL;
        """
    )
    cur.execute(
        """
        WITH dead AS (
          UPDATE browser_tasks
          SET status = 'dead_letter', locked_by = NULL, lease_expires_at = NULL,
              finished_at = now(),
              error_message = COALESCE(error_message, 'Max retries exceeded')
          WHERE status = 'running'
            AND lease_expires_at < now()
            AND execution_state = 'not_started'
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
            AND retry_count <= max_retries
          ORDER BY
            CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
            created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        RETURNING id::text, task_type, application_id::text,
                  input_json, timeout_seconds, retry_count, max_retries,
                  approval_request_id::text, expected_origin,
                  generated_document_id::text, document_sha256,
                  bound_artifact_id::text, artifact_sha256, artifact_filename,
                  expected_initial_url, expected_page_fingerprint, autofill_input_hash,
                  autofill_action_scope;
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
        "approval_request_id": row[7], "expected_origin": row[8],
        "generated_document_id": row[9], "document_sha256": row[10],
        "bound_artifact_id": row[11], "artifact_sha256": row[12], "artifact_filename": row[13],
        "expected_initial_url": row[14], "expected_page_fingerprint": row[15], "autofill_input_hash": row[16],
        "autofill_action_scope": row[17] or {},
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


def close_terminal_capability_if_provably_pre_io(cur, task: Dict[str, Any], reason: str) -> None:
    """Expire a terminal capability only when durable state proves zero I/O."""
    if task.get("task_type") != "fill_application_form" or not task.get("approval_request_id"):
        return
    cur.execute(
        """
        SELECT execution_state,
               EXISTS (SELECT 1 FROM autofill_action_journal j WHERE j.browser_task_id = b.id)
          FROM browser_tasks b
         WHERE b.id = %s
         FOR UPDATE;
        """,
        (task["id"],),
    )
    row = cur.fetchone()
    if not row:
        return
    execution_state, has_journal = row
    if has_journal or execution_state in {"partial", "completed", "needs_reconciliation"}:
        return
    if execution_state == "executing":
        cur.execute(
            """UPDATE browser_tasks
                  SET execution_state = 'not_started'
                WHERE id = %s AND execution_state = 'executing';""",
            (task["id"],),
        )
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'failed', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": reason[:500]}), task["id"]),
        )
    _expire_bound_pre_io_approval(cur, task, reason)


def requeue_or_fail(cur, task: Dict[str, Any], error: str) -> str:
    """Retry only when durable state proves that no browser write can replay."""
    new_count = task["retry_count"] + 1
    exhausted = new_count > task["max_retries"]

    if task.get("task_type") == "fill_application_form":
        cur.execute(
            """
            SELECT execution_state,
                   EXISTS (SELECT 1 FROM autofill_action_journal j WHERE j.browser_task_id = b.id)
              FROM browser_tasks b
             WHERE b.id = %s
             FOR UPDATE;
            """,
            (task["id"],),
        )
        row = cur.fetchone()
        execution_state, has_journal = row if row else ("needs_reconciliation", True)

        if has_journal or execution_state in {"partial", "completed", "needs_reconciliation"}:
            cur.execute(
                """
                UPDATE browser_tasks
                SET status = 'failed', execution_state = 'needs_reconciliation',
                    retry_count = %s, error_message = %s,
                    locked_by = NULL, lease_expires_at = NULL, finished_at = now()
                WHERE id = %s;
                """,
                (new_count, error[:2000], task["id"]),
            )
            cur.execute(
                """UPDATE application_attempts
                      SET status = 'needs_review', finished_at = COALESCE(finished_at, now()),
                          detail_json = detail_json || %s
                    WHERE browser_task_id = %s AND status = 'started';""",
                (Jsonb({"reason": error[:500]}), task["id"]),
            )
            return "failed"

        if execution_state == "executing":
            # No journal means the durable before-action record was never
            # written, so external I/O is provably absent. Release the exact
            # executing capability before retrying.
            cur.execute(
                """
                UPDATE approval_requests
                SET status = 'approved', executing_task_id = NULL
                WHERE id = %s AND application_id = %s
                  AND status = 'executing' AND executing_task_id = %s
                  AND consumed_at IS NULL
                RETURNING id;
                """,
                (task.get("approval_request_id"), task.get("application_id"), task["id"]),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """UPDATE browser_tasks
                          SET status = 'failed', execution_state = 'needs_reconciliation',
                              retry_count = %s, error_message = %s,
                              locked_by = NULL, lease_expires_at = NULL, finished_at = now()
                        WHERE id = %s;""",
                    (new_count, error[:2000], task["id"]),
                )
                return "failed"
            cur.execute(
                "UPDATE browser_tasks SET execution_state = 'not_started' WHERE id = %s;",
                (task["id"],),
            )
            cur.execute(
                """UPDATE application_attempts
                      SET status = 'failed', finished_at = COALESCE(finished_at, now()),
                          detail_json = detail_json || %s
                    WHERE browser_task_id = %s AND status = 'started';""",
                (Jsonb({"reason": "worker failed before first browser action; safe retry"}), task["id"]),
            )

        if exhausted:
            _expire_bound_pre_io_approval(cur, task, f"Autofill retries exhausted before browser I/O: {error}")

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

def require_verified_document(cur, document_id: str, application_id: Optional[str]) -> Dict[str, Any]:
    """The single chokepoint that keeps L3 from writing ungrounded text."""
    if not application_id:
        raise PermanentTaskError("A form-write document must have an application_id.")
    cur.execute(
        """
        SELECT id::text, doc_type, content, qa_status, approved
        FROM generated_documents
        WHERE id = %s AND application_id = %s;
        """,
        (document_id, application_id),
    )
    row = cur.fetchone()
    if not row:
        raise PermanentTaskError(
            f"generated_document {document_id} does not belong to application {application_id}."
        )

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


def _normalised_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PermanentTaskError(f"Invalid expected origin: {url[:120]}")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def require_bound_approval(cur, task: Dict[str, Any]) -> Dict[str, Any]:
    """Load exactly one unused capability bound to this task, app and document.

    Looking up *any* approved request for an application was an authorization
    bypass: a stale approval could authorize another URL/document.  Legacy
    tasks have no complete binding and intentionally fail closed.
    """
    application_id = task.get("application_id")
    approval_id = task.get("approval_request_id")
    document_id = task.get("generated_document_id") or task.get("input_json", {}).get("generated_document_id")
    expected_origin = task.get("expected_origin")
    document_sha256 = task.get("document_sha256")
    if not application_id:
        raise PermanentTaskError(
            "Task touches a real application form but has no application_id."
        )
    if not all((approval_id, document_id, expected_origin, document_sha256)):
        raise PermanentTaskError(
            "Form-write task lacks an exact approval_request_id, document id/hash, or expected origin. "
            "Legacy approvals cannot authorize a form write."
        )
    origin = _normalised_origin(str(expected_origin))
    cur.execute(
        """
        SELECT id::text, target_action, bound_document_id::text,
               bound_document_sha256, expected_origin, bound_artifact_id::text,
               bound_artifact_sha256, bound_artifact_filename, expected_initial_url,
               expected_page_fingerprint, bound_autofill_input_hash, bound_autofill_action_scope
        FROM approval_requests
        WHERE id = %s
          AND application_id = %s
          AND status = 'approved'
          AND consumed_at IS NULL
          AND token_expires_at > now()
        """,
        (approval_id, application_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PermanentTaskError(
            "No valid unused approval exists for this exact application capability."
        )
    approved_id, target_action, bound_doc, bound_hash, bound_origin, artifact_id, artifact_hash, artifact_filename, page_url, page_fingerprint_sha, input_hash, action_scope = row
    if target_action != "fill_application_form":
        raise PermanentTaskError(f"Approval {approved_id} is not for fill_application_form.")
    if bound_doc != document_id or bound_hash != document_sha256:
        raise PermanentTaskError("Approval document binding does not match this task.")
    if _normalised_origin(str(bound_origin or "")) != origin:
        raise PermanentTaskError("Approval origin binding does not match this task.")
    if (artifact_id or task.get("bound_artifact_id")) and (
        artifact_id != task.get("bound_artifact_id") or artifact_hash != task.get("artifact_sha256")
        or artifact_filename != task.get("artifact_filename")
    ):
        raise PermanentTaskError("Approval artifact binding does not match this task.")
    try:
        page_matches = canonical_page_url(str(page_url)) == canonical_page_url(str(task.get("expected_initial_url") or ""))
    except ValueError:
        page_matches = False
    if not all((page_url, page_fingerprint_sha, input_hash)) or not page_matches or (
        page_fingerprint_sha != task.get("expected_page_fingerprint")
        or input_hash != task.get("autofill_input_hash")
        or (action_scope or {}) != (task.get("autofill_action_scope") or {})
    ):
        raise PermanentTaskError("Approval page/input binding does not match this task.")
    return {"id": approved_id, "document_id": document_id, "expected_origin": origin,
            "artifact_id": artifact_id, "artifact_sha256": artifact_hash, "artifact_filename": artifact_filename,
            "expected_initial_url": page_url, "expected_page_fingerprint": page_fingerprint_sha,
            "autofill_input_hash": input_hash, "autofill_action_scope": action_scope or {}}


def require_current_input_hash(binding: Dict[str, Any], current_hash: str) -> None:
    """Reject a capability when profile/legal/document inputs changed post-approval."""
    if str(binding.get("autofill_input_hash") or "") != str(current_hash or ""):
        raise PermanentTaskError(
            "Candidate profile or confirmed legal answer changed after approval; issue a new approval."
        )


def action_is_in_approved_scope(action, scope: Dict[str, Any]) -> bool:
    """Do not let a dynamic ATS reveal an unreviewed extra write."""
    if action.action not in {"fill", "select", "check", "upload"}:
        return True
    key = action.profile_key or ""
    if key.startswith("documents."):
        return key.removeprefix("documents.") in set(scope.get("document_types") or [])
    if key:
        return key in set(scope.get("profile_keys") or [])
    question = action.question_label or ""
    semantic = classify_immigration_question(question)
    if semantic is not None:
        return semantic.value in set(scope.get("sensitive_classes") or [])
    return normalize_question(question) in set(scope.get("remembered_questions") or [])


def _durable_connection():
    """A commit independent of the worker's task transaction.

    Browser writes cannot roll back.  These small state changes therefore
    deliberately survive a later rollback of the main queue transaction.
    """
    # A dedicated connection is durable relative to the queue worker's outer
    # transaction, but its related statements must still commit atomically.
    return psycopg.connect(DSN, autocommit=False)


def durable_begin_execution(task: Dict[str, Any], binding: Dict[str, Any], target_id: str) -> None:
    """Move the capability to executing before the first browser write."""
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE approval_requests
            SET status = 'executing', executing_task_id = %s
            WHERE id = %s AND application_id = %s
              AND status = 'approved' AND consumed_at IS NULL
              AND token_expires_at > now()
            RETURNING id;
            """,
            (task["id"], binding["id"], task["application_id"]),
        )
        if cur.fetchone() is None:
            raise PermanentTaskError("Approval changed before execution; create a new approval instead of replaying.")
        cur.execute(
            """
            UPDATE browser_tasks
            SET execution_state = 'executing', pinned_target_id = %s
            WHERE id = %s AND status = 'running';
            """,
            (target_id, task["id"]),
        )
        if cur.rowcount != 1:
            raise PermanentTaskError("Browser task is no longer running; refusing a browser write.")
        cur.execute(
            """INSERT INTO application_attempts
                  (application_id, browser_task_id, attempt_kind, status, detail_json)
               VALUES (%s, %s, 'deterministic_autofill', 'started', %s);""",
            (task["application_id"], task["id"], Jsonb({"pinned_target_id": target_id})),
        )


def durable_journal_start(task: Dict[str, Any], binding: Dict[str, Any], action, target_id: str) -> str:
    """Persist a pending external action before invoking OpenClaw."""
    value_hash = hashlib.sha256((action.value or "").encode("utf-8")).hexdigest()
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO autofill_action_journal
              (browser_task_id, approval_request_id, sequence_no, target_id,
               action_kind, target_ref, expected_value_sha256, status)
            SELECT %s, %s, COALESCE(MAX(sequence_no), 0) + 1, %s,
                   %s, %s, %s, 'started'
            FROM autofill_action_journal
            WHERE browser_task_id = %s
            RETURNING id::text;
            """,
            (task["id"], binding["id"], target_id, action.action, action.ref, value_hash, task["id"]),
        )
        return cur.fetchone()[0]


def durable_journal_verified(action, target_id: str, journal_id: str) -> None:
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE autofill_action_journal
            SET status = 'verified', verified_at = now(),
                observed_json = %s
            WHERE id = %s AND status = 'started';
            """,
            (Jsonb({"target_id": target_id, "ref": action.ref, "action": action.action}), journal_id),
        )
        if cur.rowcount != 1:
            raise PermanentTaskError("Autofill action journal changed unexpectedly; manual reconciliation is required.")


def durable_journal_failed(action, target_id: str, journal_id: str) -> None:
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE autofill_action_journal
            SET status = 'failed', observed_json = %s
            WHERE id = %s AND status = 'started';
            """,
            (Jsonb({"target_id": target_id, "ref": action.ref, "action": action.action}), journal_id),
        )


def durable_finish_execution(task: Dict[str, Any], binding: Dict[str, Any], result) -> None:
    """Consume once only after the session finishes, including partial writes.

    A partially written form must never be retried under the same capability.
    The journal is the recovery record for the user/reviewer.
    """
    execution_state = "completed" if result.status == "completed" else "partial"
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE approval_requests
            SET status = 'consumed', consumed_at = now(), consumed_by = %s,
                executing_task_id = NULL
            WHERE id = %s AND application_id = %s
              AND status = 'executing' AND executing_task_id = %s
            RETURNING id;
            """,
            (WORKER_ID, binding["id"], task["application_id"], task["id"]),
        )
        if cur.fetchone() is None:
            raise PermanentTaskError("Executing approval changed unexpectedly; manual reconciliation is required.")
        cur.execute(
            """
            UPDATE browser_tasks
            SET execution_state = %s, pinned_target_id = %s
            WHERE id = %s;
            """,
            (execution_state, result.target_id, task["id"]),
        )
        cur.execute(
            """UPDATE application_attempts
                   SET status = %s, finished_at = now(),
                       detail_json = detail_json || %s
                 WHERE browser_task_id = %s AND status = 'started';""",
            (execution_state, Jsonb({"verified_refs": list(result.verified_refs),
                                     "failed_refs": list(result.failed_refs)}), task["id"]),
        )
        if execution_state == "completed":
            cur.execute(
                """UPDATE applications
                      SET current_step = 'form_filled', updated_at = now()
                    WHERE id = %s AND current_step = 'awaiting_approval';""",
                (task["application_id"],),
            )
            if cur.rowcount == 1:
                cur.execute(
                    """INSERT INTO pipeline_events(
                           application_id, from_step, to_step, actor, reason, detail_json)
                       VALUES (%s, 'awaiting_approval', 'form_filled', %s,
                               'Deterministic autofill completed; final submit remains human-only.', %s);""",
                    (task["application_id"], WORKER_ID, Jsonb({"browser_task_id": task["id"]})),
                )


def durable_mark_reconciliation(task: Dict[str, Any], target_id: str | None, reason: str) -> None:
    """Leave an executing capability non-replayable after an uncertain write."""
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE browser_tasks
            SET execution_state = 'needs_reconciliation', pinned_target_id = COALESCE(%s, pinned_target_id),
                error_message = %s
            WHERE id = %s;
            """,
            (target_id, reason[:2000], task["id"]),
        )
        cur.execute(
            """UPDATE application_attempts
                   SET status = 'needs_review', finished_at = now(),
                       detail_json = detail_json || %s
                 WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": reason[:500]}), task["id"]),
        )


def durable_close_unstarted_approval(task: Dict[str, Any], binding: Dict[str, Any], reason: str) -> None:
    """Close a capability that produced no external browser write.

    An approved capability must not remain live after its queue task reached a
    no-write terminal state (for example, every proposed field needed review).
    A later profile or form change must receive a fresh approval instead.
    """
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE approval_requests
            SET status = 'expired', action_note = COALESCE(action_note, %s)
            WHERE id = %s AND application_id = %s
              AND status = 'approved' AND consumed_at IS NULL;
            """,
            (reason[:500], binding["id"], task["application_id"]),
        )
        if cur.rowcount != 1:
            raise PermanentTaskError("Unstarted approval changed unexpectedly; do not reuse it.")


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
    """Require an HTTP(S) URL and enforce the central allowed-domain gate."""
    url = (input_json.get("url") or "").strip()
    if not url:
        raise PermanentTaskError("input_json.url is required.")
    if not url.startswith(("http://", "https://")):
        raise PermanentTaskError(f"Refusing non-http(s) URL: {url[:120]}")
    check_domain(cur, url)
    return url


def require_ats_autofill_capability(cur, application_id: str) -> dict[str, bool]:
    """Fail closed when an ATS has not been explicitly proven for this mode."""
    cur.execute("SELECT coalesce(ats_type, 'unknown') FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    ats_type = str(row[0]) if row else "unknown"
    cur.execute("""SELECT autofill_mode, supports_static_text, supports_radio,
                          supports_select, supports_upload
                   FROM ats_capabilities WHERE ats_type = %s;""", (ats_type,))
    capability = cur.fetchone()
    if not capability or capability[0] == "review_only":
        raise PermanentTaskError(
            f"ATS '{ats_type}' is review-only or unregistered. Add a proven single-page capability before browser writes."
        )
    return {"fill": bool(capability[1]), "check": bool(capability[2]),
            "select": bool(capability[3]), "upload": bool(capability[4])}


def snapshot_state(transport: OpenClawTransport, target_id: str) -> SnapshotState:
    payload = transport.snapshot(target_id)
    nodes = parse_snapshot(payload)
    return SnapshotState(
        tuple(inspect_nodes(nodes)), tuple(inspect_question_groups(nodes)),
        page_fingerprint(payload, page_url=transport.current_url(target_id)), bool(payload.get("truncated")),
    )


# ---------------------------------------------------------------- handlers

def handle_fetch_job_description(cur, task) -> Dict[str, Any]:
    url = require_url(cur, task["input_json"])
    # The third LinkedIn intake mode is intentionally deterministic: the user
    # opens one already-authenticated job page, then JobOS reads the exact
    # pinned tab without giving an LLM a browser handle.
    if task["input_json"].get("source") == "linkedin" and task["input_json"].get("deterministic_read_only"):
        if not feature_enabled("JOBOS_LINKEDIN_READONLY_CAPTURE_ENABLED"):
            raise PermanentTaskError("LinkedIn deterministic read-only capture is disabled by configuration.")
        transport = OpenClawTransport(
            binary=OPENCLAW_BIN, profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"),
            timeout=min(int(task["timeout_seconds"]), 90), environment=openclaw_runtime_env(),
        )
        target = transport.resolve_target()
        current = transport.current_url(target.target_id)
        try:
            exact_current, exact_requested = validate_job_url(current), validate_job_url(url)
        except LinkedInDiscoveryError as exc:
            raise PermanentTaskError(f"Pinned page is not a canonical LinkedIn job detail: {exc}") from exc
        if exact_current != exact_requested:
            raise PermanentTaskError(
                "Pinned browser tab is not the user-approved LinkedIn job URL; open that exact job page and retry."
            )
        payload = transport.snapshot(target.target_id)
        if payload.get("truncated"):
            raise PermanentTaskError("LinkedIn snapshot is truncated; refusing partial JD capture.")
        text = str(payload.get("snapshot") or "").strip()
        if not text:
            raise PermanentTaskError("Pinned LinkedIn job page produced no readable snapshot.")
        return {
            "url": current, "mode": "linkedin_deterministic_read_only",
            "pinned_target_id": target.target_id, "agent_response": {"text": text},
            "submitted": False,
        }
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


def handle_discover_linkedin_jobs(cur, task) -> Dict[str, Any]:
    """Search a linked LinkedIn profile under a small user-requested quota.

    The search happens only for a queued task bearing user_initiated=true.
    The agent is not allowed to authenticate, create alerts, save jobs,
    message anyone, alter preferences, or apply; it merely reads at most the
    requested number of result detail pages and returns their JDs as JSON.
    """
    if not feature_enabled("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED"):
        raise PermanentTaskError(
            "LinkedIn autonomous discovery is disabled. Enable "
            "JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED only for a dedicated, manually logged-in JobOS browser."
        )
    inp = task["input_json"]
    if inp.get("user_initiated") is not True:
        raise PermanentTaskError("LinkedIn discovery requires explicit user_initiated=true.")
    try:
        request = validate_search_request(
            str(inp.get("keywords") or ""), str(inp.get("location") or ""), inp.get("max_results"),
            date_posted=inp.get("date_posted"), experience_levels=inp.get("experience_levels"),
            employment_types=inp.get("employment_types"), work_modes=inp.get("work_modes"),
            companies=inp.get("companies"), sort_by=inp.get("sort_by"),
        )
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(str(exc)) from exc
    search_url = "https://www.linkedin.com/jobs/search/?" + urlencode({
        "keywords": request["keywords"], "location": request["location"],
    })
    check_domain(cur, search_url)
    msg = (
        "Use the OpenClaw browser tool with profile exactly `remote`; it is the attach-only "
        "JobOS Chrome CDP profile that is already signed in to LinkedIn. Never use or start "
        "a profile named `linkedin`, `work`, `openclaw`, or `chrome`. "
        "Search the URL below. "
        "This is a bounded, read-only user-requested discovery task.\n"
        f"Search URL: {search_url}\n"
        f"Read no more than {request['max_results']} result detail pages. Apply only these requested filters when LinkedIn exposes them: "
        f"date_posted={request['date_posted'] or 'any'}, experience_levels={request['experience_levels'] or ['any']}, "
        f"employment_types={request['employment_types'] or ['any']}, work_modes={request['work_modes'] or ['any']}, "
        f"companies={request['companies'] or ['any']}, sort={request['sort_by']}.\n\n"
        "First list/focus tabs for profile `remote`. If a LinkedIn jobs-search tab "
        "already has these keywords and location, snapshot it and do not navigate again. "
        "If navigation reports a timeout, immediately list tabs and snapshot the current "
        "page: LinkedIn may have completed navigation even though its background requests "
        "did not become idle. Never treat a navigation timeout alone as a failed page.\n\n"
        "Do not authenticate, use credentials, solve CAPTCHA, change job preferences, "
        "create alerts, save jobs, message anyone, upload, fill fields, click Easy Apply, "
        "or submit anything. If the existing browser session is not signed in or a "
        "CAPTCHA appears, stop and report that exact blocker.\n\n"
        "Open at most the requested number of eligible result details. Snapshot each detail pane "
        "and copy its complete visible `About the job` text. "
        "Use a canonical URL exactly like https://www.linkedin.com/jobs/view/<numeric-id>/; "
        "discard tracking query parameters.\n\n"
        "For each read result return ONLY one JSON object in this exact form:\n"
        "{\"jobs\":[{\"company\":\"...\",\"title\":\"...\",\"location\":\"...\","
        "\"work_mode\":\"remote|hybrid|on-site|unknown\",\"url\":\"https://www.linkedin.com/jobs/...\","
        "\"jd_text\":\"full visible job description text\"}]}\n"
        "Include only pages with a full visible JD of at least 200 characters. Do not "
        "summarise, infer, invent a URL, or include jobs beyond the cap. If extraction "
        "cannot be grounded in the snapshot, return {\"jobs\":[]} rather than guessing."
    )

    try:
        agent_response = openclaw_agent(
            agent=OPENCLAW_AGENT_LINKEDIN_DISCOVERY, message=msg, timeout=task["timeout_seconds"],
            session_id=f"jobos-task-{task['id']}",
        )
    except RuntimeError as exc:
        raise TransientTaskError(str(exc)) from exc

    # Discovery is read-only. Login, checkpoint, CAPTCHA, or verification is
    # a terminal human-intervention boundary: never retry or alter LinkedIn.
    blocker_text = json.dumps(agent_response, ensure_ascii=False).casefold()
    if any(marker in blocker_text for marker in (
        "captcha", "verification", "security check", "checkpoint",
        "sign in", "login required",
    )):
        raise PermanentTaskError(
            "LinkedIn discovery stopped for human login/CAPTCHA/verification handling; "
            "no retry or LinkedIn state change was attempted."
        )

    # =====================================================================
    # 3. LƯU VÀO DB
    # =====================================================================
    try:
        intake = ingest_discovered_jobs(cur, task["id"], inp, agent_response)
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(f"LinkedIn discovery result refused: {exc}") from exc

    return {
        "search_url": search_url, "search": request, "submitted": False,
        "auto_ingest": intake, "agent_response": agent_response,}


def handle_discover_linkedin_saved_jobs(cur, task) -> Dict[str, Any]:
    """Read only the jobs the user has already saved on LinkedIn.

    This is deliberately a separate handler: it does not modify the existing
    LinkedIn discovery, parallel bypass, CAPTCHA detection, or CapSolver flow.
    """
    if not feature_enabled("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED"):
        raise PermanentTaskError("LinkedIn Saved Jobs discovery is disabled by configuration.")
    inp = task["input_json"]
    if inp.get("user_initiated") is not True:
        raise PermanentTaskError("Saved Jobs sync requires explicit user_initiated=true.")
    try:
        request = validate_saved_request(inp.get("max_results"))
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(str(exc)) from exc
    saved_url = "https://www.linkedin.com/my-items/saved-jobs/"
    check_domain(cur, saved_url)
    sync_id = str(inp.get("saved_sync_id") or "").strip() or None
    if sync_id:
        cur.execute(
            """UPDATE linkedin_saved_syncs
                  SET status = 'running', started_at = coalesce(started_at, now()), error_message = NULL
                WHERE id = %s;""",
            (sync_id,),
        )
    msg = (
        "Use the OpenClaw browser tool with profile exactly `remote`, already manually authenticated. "
        "Open/read this LinkedIn Saved Jobs page only. This task is READ ONLY.\n"
        f"Saved Jobs URL: {saved_url}\n"
        f"Read at most {request['max_results']} existing saved job postings and their complete visible job descriptions.\n\n"
        "Never authenticate, save/unsave, apply, message, upload, fill fields, submit, or change LinkedIn preferences. "
        "If login or a checkpoint appears, stop and report it.\n"
        "Return ONLY JSON: {\"jobs\":[{\"company\":\"...\",\"title\":\"...\",\"location\":\"...\","
        "\"work_mode\":\"remote|hybrid|on-site|unknown\",\"url\":\"https://www.linkedin.com/jobs/view/<numeric-id>/\","
        "\"jd_text\":\"full visible job description\"}]}. "
        "Each record needs a grounded canonical URL and 200+ JD characters."
    )
    try:
        agent_response = openclaw_agent(
            agent=OPENCLAW_AGENT_LINKEDIN_DISCOVERY, message=msg,
            timeout=task["timeout_seconds"], session_id=f"jobos-saved-{task['id']}",
        )
    except RuntimeError as exc:
        raise TransientTaskError(str(exc)) from exc
    blocker_text = json.dumps(agent_response, ensure_ascii=False).casefold()
    if any(word in blocker_text for word in ("captcha", "checkpoint", "sign in", "login required")):
        raise PermanentTaskError("LinkedIn Saved Jobs requires human login/checkpoint handling; no LinkedIn state changed.")
    try:
        intake = ingest_saved_jobs(cur, task["id"], inp, agent_response)
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(f"LinkedIn Saved Jobs result refused: {exc}") from exc
    return {"saved_url": saved_url, "saved": request, "submitted": False,
            "auto_ingest": intake, "agent_response": agent_response}


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
    action_capabilities = require_ats_autofill_capability(cur, task["application_id"])
    binding = require_bound_approval(cur, task)
    document = require_verified_document(cur, binding["document_id"], task["application_id"])
    document_hash = hashlib.sha256((document["content"] or "").encode("utf-8")).hexdigest()
    if document_hash != task["document_sha256"]:
        raise PermanentTaskError("Bound generated document content changed after approval; reissue approval.")

    try:
        context = load_autofill_context(
            cur, application_id=task["application_id"], artifact_binding=binding,
            document_sha256=document_hash, page_url=binding["expected_initial_url"],
            page_fingerprint_sha256=binding["expected_page_fingerprint"], data_root=REPO_ROOT / "data",
        )
    except AutofillContextError as exc:
        raise PermanentTaskError(str(exc)) from exc
    require_current_input_hash(binding, context.input_hash)
    approved_upload_hashes = {}
    if binding.get("artifact_id") and binding.get("artifact_sha256"):
        for approved_path in (context.profile.get("documents") or {}).values():
            if approved_path:
                approved_upload_hashes[str(Path(str(approved_path)).expanduser().resolve())] = str(binding["artifact_sha256"])
    transport = OpenClawTransport(
        binary=OPENCLAW_BIN, profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"),
        timeout=min(int(task["timeout_seconds"]), 90), environment=openclaw_runtime_env(),
        approved_upload_hashes=approved_upload_hashes,
    )
    execution_started = False
    pinned_target_id: str | None = None
    latest_actions = []

    def make_plan(state: SnapshotState):
        nonlocal latest_actions
        latest_actions, _ = plan_autofill(
            list(state.fields), context.profile, question_groups=list(state.groups),
            approved_sensitive_answers=context.sensitive_answers,
            remembered_answers=context.remembered_answers,
        )
        latest_actions = [
            action if action_is_in_approved_scope(action, binding["autofill_action_scope"]) and
                      (action.action not in action_capabilities or action_capabilities[action.action])
            else type(action)("pause", action.ref, None, action.profile_key,
                              "Action is outside the approved scope or unsupported by this ATS.", action.question_label)
            for action in latest_actions
        ]
        return latest_actions

    def begin_execution(target_id: str) -> None:
        nonlocal execution_started, pinned_target_id
        durable_begin_execution(task, binding, target_id)
        execution_started, pinned_target_id = True, target_id

    try:
        session = AutofillSession(
            transport=transport, expected_origin=binding["expected_origin"],
            expected_initial_url=binding["expected_initial_url"],
            expected_page_fingerprint=binding["expected_page_fingerprint"],
            snapshot_state=lambda target_id: snapshot_state(transport, target_id),
            origin_allowed=lambda url: check_domain(cur, url),
            begin_execution=begin_execution,
            before_action=lambda action, target_id: durable_journal_start(task, binding, action, target_id),
            after_verified=durable_journal_verified,
            after_failed=durable_journal_failed,
        )
        result = session.execute(make_plan)
        if execution_started:
            durable_finish_execution(task, binding, result)
        else:
            durable_close_unstarted_approval(
                task, binding,
                "No deterministic browser write ran; issue a fresh approval after review or form changes.",
            )
    except (SessionError, TransportError, PermanentTaskError) as exc:
        if execution_started:
            durable_mark_reconciliation(task, pinned_target_id, str(exc))
            raise PermanentTaskError(
                "Browser execution entered an uncertain state after a write; "
                "it will not retry automatically. Review the autofill action journal."
            ) from exc
        if isinstance(exc, TransportError):
            raise TransientTaskError(str(exc)) from exc
        # This task is terminal but never wrote to the browser.  Close its
        # capability so an invalid page identity or changed form can be
        # reviewed and approved afresh instead of leaving idempotency stuck.
        durable_close_unstarted_approval(task, binding, f"Preflight refused: {exc}")
        raise PermanentTaskError(str(exc)) from exc
    screenshot_path = None
    try:
        # This is deliberately performed only after the deterministic session
        # is closed. A failed review artifact must never replay browser writes.
        captured = transport.screenshot(result.target_id, full_page=True)
        durable_dir = REPO_ROOT / "data" / "review-artifacts" / str(task["application_id"]) / str(task["id"])
        durable_dir.mkdir(parents=True, exist_ok=True)
        durable = durable_dir / "autofill.png"
        shutil.copy2(captured, durable)
        durable.chmod(0o600)
        screenshot_path = str(durable.resolve())
        cur.execute("UPDATE browser_tasks SET screenshot_url = %s WHERE id = %s;", (screenshot_path, task["id"]))
    except Exception as screenshot_exc:
        # Screenshot capture is a post-execution review artifact, not part of
        # the deterministic browser write transaction.  Even an unexpected
        # capture/serialization failure must not reclassify already-verified
        # writes as uncertain or trigger replay/reconciliation.
        print(
            "  warning: post-autofill screenshot capture failed; "
            f"browser writes remain final: {type(screenshot_exc).__name__}: {screenshot_exc}"
        )
        screenshot_path = None
    return {
        "status": result.status, "verified_refs": list(result.verified_refs),
        "failed_refs": list(result.failed_refs), "executed_refs": list(result.executed_refs),
        "pinned_target_id": result.target_id,
        "paused": [action.question_label or action.reason for action in latest_actions if action.action == "pause"],
        "approval_consumed": execution_started,
        "approval_closed_without_write": not execution_started,
        "screenshot_path": screenshot_path,
        "submitted": False, }


HANDLERS = {
    "fetch_job_description": handle_fetch_job_description,
    "discover_linkedin_jobs": handle_discover_linkedin_jobs,
    "discover_linkedin_saved_jobs": handle_discover_linkedin_saved_jobs,
    "capture_page_snapshot": handle_capture_page_snapshot,
    "fill_application_form": handle_fill_application_form,
}


def update_saved_sync_failure(cur, task: Dict[str, Any], status: str, error: str) -> None:
    """Keep the Saved Jobs sync record aligned with the browser task lifecycle."""
    if task.get("task_type") != "discover_linkedin_saved_jobs":
        return
    sync_id = str((task.get("input_json") or {}).get("saved_sync_id") or "").strip()
    if not sync_id:
        return
    if status == "queued":
        cur.execute(
            """UPDATE linkedin_saved_syncs SET status = 'queued', error_message = %s
                 WHERE id = %s;""",
            (error[:2000], sync_id),
        )
    elif status == "failed":
        cur.execute(
            """UPDATE linkedin_saved_syncs SET status = 'failed', error_message = %s,
                      completed_at = now() WHERE id = %s;""",
            (error[:2000], sync_id),
        )


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
                require_bound_approval(cur, task)

            start = time.perf_counter()
            result = HANDLERS[task["task_type"]](cur, task)
            elapsed = time.perf_counter() - start

            result["worker_version"] = WORKER_VERSION
            result["elapsed_seconds"] = round(elapsed, 1)
            complete_task(cur, task["id"], result)
        # Browser completion is a durable boundary of its own.  A failure in
        # Human Review materialization must never reclassify already-verified
        # browser writes as uncertain or send the task through retry logic.
        conn.commit()

        if task["task_type"] == "fill_application_form":
            try:
                with conn.cursor() as cur:
                    from services.review.review_service_v1 import ensure_autofill_review
                    ensure_autofill_review(
                        cur, task["id"], screenshot_path=result.get("screenshot_path"), result=result,
                    )
                conn.commit()
            except Exception as review_exc:
                conn.rollback()
                # sync_inbox() can deterministically recreate this materialized
                # review from the completed browser task later.  Do not replay
                # or reconcile browser side effects merely because the UI layer
                # failed to materialize.
                print(
                    "  warning: autofill completed but review materialization "
                    f"was deferred: {type(review_exc).__name__}: {review_exc}"
                )

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
            close_terminal_capability_if_provably_pre_io(cur, task, str(e))
            fail_task(cur, task["id"], str(e))
            update_saved_sync_failure(cur, task, "failed", str(e))
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
            update_saved_sync_failure(cur, task, status, str(e))
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
            update_saved_sync_failure(cur, task, status, f"{type(e).__name__}: {e}")
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
            gateway_ok, out = openclaw_health()
        except (PermanentTaskError, TransientTaskError) as e:
            print(f"FAIL: {e}")
            return 1
        print(out)
        cdp_ok, cdp_out = browser_cdp_health()
        print("\nRPC reachable" if gateway_ok else "\nRPC NOT reachable")
        print(cdp_out)
        return 0 if gateway_ok and cdp_ok else 1

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
