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
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
from pathlib import Path
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly. Without this, `from services.common...` below raises
# ModuleNotFoundError unless the caller happens to have the repo root on
# PYTHONPATH already. Confirmed live 2026-08-01.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.observability import emit_trace, make_trace_id
from services.discovery.linkedin_discovery_v1 import (
    LinkedInDiscoveryError,
    ingest_discovered_jobs,
    validate_search_request,
)

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

# Prefer the pinned JobOS runtime when setup installed it.  A global OpenClaw
# can otherwise inherit an unsupported Node version or a different plugin set.
REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_OPENCLAW_BIN = REPO_ROOT / "data" / "openclaw-runtime" / "node" / "node_modules" / ".bin" / "openclaw"
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN") or (str(PRIVATE_OPENCLAW_BIN) if PRIVATE_OPENCLAW_BIN.is_file() else "openclaw")
# Agent ids as defined in ~/.openclaw/openclaw.json -> agents.list
OPENCLAW_AGENT_BROWSE = os.getenv("OPENCLAW_AGENT_BROWSE", "main")
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

WORKER_VERSION = "browser_queue_worker_v3_fail_closed_form_write_2026_08_23"
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
LEASE_SECONDS = int(os.getenv("JOBOS_BROWSER_LEASE_SECONDS", "600"))

SUPPORTED_TASKS = {
    "fetch_job_description",
    "discover_linkedin_jobs",
    "capture_page_snapshot",
    "fill_application_form",
}
REQUIRES_APPROVAL = {"fill_application_form"}


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
    """Archive exhausted tasks using the live-to-archive error-column mapping.

    Fix note: ``browser_tasks`` returns ``error_message`` whereas
    ``dead_letter_tasks`` stores it as ``last_error``. The explicit mapping
    prevents the historical CTE column error recorded in the Windows log.
    """
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
                  input_json, timeout_seconds, retry_count, max_retries,
                  approval_request_id::text, expected_origin,
                  generated_document_id::text, document_sha256;
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
               bound_document_sha256, expected_origin
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
    approved_id, target_action, bound_doc, bound_hash, bound_origin = row
    if target_action != "fill_application_form":
        raise PermanentTaskError(f"Approval {approved_id} is not for fill_application_form.")
    if bound_doc != document_id or bound_hash != document_sha256:
        raise PermanentTaskError("Approval document binding does not match this task.")
    if _normalised_origin(str(bound_origin or "")) != origin:
        raise PermanentTaskError("Approval origin binding does not match this task.")
    return {"id": approved_id, "document_id": document_id, "expected_origin": origin}


def consume_bound_approval(cur, approval_id: str, task: Dict[str, Any]) -> None:
    """Atomically consume one capability after a verified side effect.

    This function is deliberately separate from token redemption.  No current
    handler calls it because form writes are disabled below; a future
    deterministic handler must call it in the same transaction as its final
    post-write verification.
    """
    cur.execute(
        """
        UPDATE approval_requests
        SET status = 'consumed', consumed_at = now(), consumed_by = %s
        WHERE id = %s AND application_id = %s
          AND status = 'approved' AND consumed_at IS NULL
        RETURNING id;
        """,
        (WORKER_ID, approval_id, task["application_id"]),
    )
    if cur.fetchone() is None:
        raise PermanentTaskError("Approval was already consumed or changed concurrently.")


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


def handle_discover_linkedin_jobs(cur, task) -> Dict[str, Any]:
    """Search a linked LinkedIn profile under a small user-requested quota.

    The search happens only for a queued task bearing user_initiated=true.
    The agent is not allowed to authenticate, create alerts, save jobs,
    message anyone, alter preferences, or apply; it merely reads at most the
    requested number of result detail pages and returns their JDs as JSON.
    """
    inp = task["input_json"]
    if inp.get("user_initiated") is not True:
        raise PermanentTaskError("LinkedIn discovery requires explicit user_initiated=true.")
    try:
        request = validate_search_request(
            str(inp.get("keywords") or ""), str(inp.get("location") or ""),
            inp.get("max_results"),
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
        f"Read no more than {request['max_results']} result detail pages.\n\n"
        "First list/focus tabs for profile `remote`. If a LinkedIn jobs-search tab "
        "already has these keywords and location, snapshot it and do not navigate again. "
        "If navigation reports a timeout, immediately list tabs and snapshot the current "
        "page: LinkedIn may have completed navigation even though its background requests "
        "did not become idle. Never treat a navigation timeout alone as a failed page.\n\n"
        "Do not authenticate, use credentials, solve CAPTCHA, change job preferences, "
        "create alerts, save jobs, message anyone, upload, fill fields, click Easy Apply, "
        "or submit anything. If the existing browser session is not signed in or a "
        "CAPTCHA appears, stop and report that exact blocker.\n\n"
        "Open only the first eligible result detail if its details are not already visible. "
        "Snapshot the detail pane and copy the complete visible `About the job` text. "
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
    agent_response = openclaw_agent(
        agent=OPENCLAW_AGENT_BROWSE, message=msg, timeout=task["timeout_seconds"],
        session_id=f"jobos-task-{task['id']}",
    )
    try:
        intake = ingest_discovered_jobs(cur, task["id"], inp, agent_response)
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(f"LinkedIn discovery result refused: {exc}") from exc
    return {
        "search_url": search_url, "search": request, "submitted": False,
        "auto_ingest": intake, "agent_response": agent_response,
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
    # This used to send an application page and applicant document to an LLM
    # browser agent.  That contradicted the deterministic L7 architecture and
    # made radio/checkbox correctness impossible to prove.  Do not re-enable
    # it until a deterministic engine re-checks the current origin before each
    # side effect, verifies the state of every control, and calls
    # consume_bound_approval() only after post-write verification.
    raise PermanentTaskError(
        "LLM-driven form filling is disabled. Use the deterministic autofill plan "
        "for review only; no browser worker may write a real application form yet."
    )


HANDLERS = {
    "fetch_job_description": handle_fetch_job_description,
    "discover_linkedin_jobs": handle_discover_linkedin_jobs,
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
                require_bound_approval(cur, task)

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
