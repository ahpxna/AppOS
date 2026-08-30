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
import importlib.util
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
# Sửa lại đoạn import ở đầu file browser_queue_worker.py
from services.autofill.parallel_bypass import execute_parallel_bypass
import threading
import requests
import websocket
from services.common.value_coercion import coerce_bool
from services.common.observability import emit_trace, make_trace_id
from services.common.config import database_dsn, load_repo_env
from services.discovery.linkedin_discovery_v1 import (
    LinkedInDiscoveryError,
    MAX_AUTONOMOUS_DISCOVERY_RESULTS, MAX_DISCOVERY_RESULTS,
    ingest_discovered_jobs,
    ingest_saved_jobs,
    blocker_safe_agent_response,
    classify_linkedin_blocker,
    validate_job_url,
    validate_search_request,
    validate_saved_request,
)
from services.autofill.autofill_agent_v1 import parse_snapshot
from services.ats.contracts import canonical_job_url
from services.autofill.autofill_executor_v1 import OpenClawTransport, TransportError
from services.autofill.autofill_planner_v1 import plan_autofill
from services.autofill.autofill_session_v1 import AutofillSession, SessionError, SnapshotState
from services.autofill.form_inspector_v1 import inspect_nodes, inspect_question_groups
from services.common.autofill_identity import canonical_page_url, page_fingerprint
from services.autofill.autofill_context_v1 import AutofillContextError, load_autofill_context
from services.common.openclaw_runtime import resolve_openclaw_binary
from services.common.immigration_semantics import classify_immigration_question
from services.common.question_memory import normalize_question
from services.common.autofill_action_scope import action_is_exactly_approved
from services.control_plane.pipeline_state import DEFAULT_PIPELINE_STATE_STORE, PipelineStateError


load_repo_env()

# ---------------------------------------------------------------- config

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
# A claimed task is never reclaimable while its bounded OpenClaw/CDP I/O is
# still permitted to run. The SQL claim also takes max(task timeout + grace).
LEASE_SECONDS = max(60, int(os.getenv("JOBOS_BROWSER_LEASE_SECONDS", "600")))
LEASE_GRACE_SECONDS = max(30, int(os.getenv("JOBOS_BROWSER_LEASE_GRACE_SECONDS", "120")))

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


def _load_fake_mouse_start():
    """Load the public FakeMouse adapter from the historical hyphenated directory."""
    path = REPO_ROOT / "services" / "browser-controller" / "fake_mouse.py"
    spec = importlib.util.spec_from_file_location("jobos_browser_fake_mouse_public", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load FakeMouse public entrypoint: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.start_fake_mouse_thread


def _start_linkedin_fake_mouse(stop_event: threading.Event):
    start_fake_mouse_thread = _load_fake_mouse_start()
    return start_fake_mouse_thread(
        "data/pointer-regimes.json",
        duration_seconds=None,
        stop_event=stop_event,
        cdp_url=BROWSER_CDP_URL.rstrip("/") + "/json",
    )


def _run_linkedin_agent_with_fake_mouse(*, message: str, timeout: int, session_id: str) -> Dict[str, Any]:
    """Run each LinkedIn scrape attempt with the existing mouse lifecycle.

    The helper intentionally does not modify FakeMouse itself; it guarantees a
    fresh public entrypoint/cleanup around both the initial scrape and a
    post-CAPTCHA retry.
    """
    stop_event = threading.Event()
    thread = None
    try:
        thread = _start_linkedin_fake_mouse(stop_event)
    except Exception as exc:
        print(f"  [FakeMouse] unavailable for this bounded attempt: {exc}")
    try:
        return openclaw_agent(agent=OPENCLAW_AGENT_LINKEDIN_DISCOVERY,
                               message=message, timeout=timeout, session_id=session_id)
    finally:
        stop_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=5)


def _cdp_runtime_evaluate(ws_url: str, expression: str) -> Any:
    """Read one value directly from a live CDP page; no LLM evidence is trusted."""
    ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=3)
    try:
        call_id = int(time.time_ns() % 1_000_000_000)
        ws.send(json.dumps({
            "id": call_id, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            message = json.loads(ws.recv())
            if isinstance(message, dict) and message.get("id") == call_id:
                if message.get("error"):
                    raise RuntimeError(f"CDP Runtime.evaluate failed: {message['error']}")
                return message.get("result", {}).get("result", {}).get("value")
        raise TimeoutError("CDP Runtime.evaluate response timed out")
    finally:
        ws.close()


def _verified_linkedin_job_evidence() -> dict[str, dict[str, Any]]:
    """Read job-bound company + Apply-link evidence directly from live LinkedIn DOM.

    External hrefs count only when their visible/accessible anchor label says
    Apply; arbitrary footer/company/ad links are not ATS enrollment evidence.
    Company identity is independently read from the job top card.
    """
    raw: dict[str, dict[str, set[str]]] = {}
    try:
        response = requests.get(BROWSER_CDP_URL.rstrip("/") + "/json", timeout=3)
        response.raise_for_status()
        tabs = response.json()
    except Exception:
        return {}
    if not isinstance(tabs, list):
        return {}
    expression = r'''(() => {
      const u = new URL(location.href);
      const pathMatch = location.pathname.match(/\/jobs\/view\/(\d+)/);
      const jobId = (pathMatch && pathMatch[1]) || u.searchParams.get('currentJobId') || '';
      const selectors = [
        '.job-details-jobs-unified-top-card__company-name a',
        '.job-details-jobs-unified-top-card__company-name',
        '.jobs-unified-top-card__company-name a',
        '.jobs-unified-top-card__company-name',
        '[data-view-name="job-details-about-company-name"]'
      ];
      let company = '';
      for (const selector of selectors) {
        const el = document.querySelector(selector);
        const text = (el && el.innerText || '').replace(/\s+/g, ' ').trim();
        if (text) { company = text; break; }
      }
      const hrefs = Array.from(document.querySelectorAll('a[href]'))
        .filter(a => {
          let parsed;
          try { parsed = new URL(a.href, location.href); } catch (_) { return false; }
          if (!/^https?:$/.test(parsed.protocol)) return false;
          const host = parsed.hostname.toLowerCase();
          if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) return false;
          if (a.hidden || a.getAttribute('aria-hidden') === 'true') return false;
          const style = window.getComputedStyle(a);
          if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || 1) === 0) return false;
          const rect = a.getBoundingClientRect();
          if (!rect || rect.width <= 0 || rect.height <= 0) return false;
          const label = [a.innerText, a.getAttribute('aria-label'), a.getAttribute('title'),
                         a.getAttribute('data-control-name')].filter(Boolean).join(' ');
          return /\bapply\b/i.test(label);
        })
        .map(a => a.href).filter(Boolean).slice(0, 50);
      return {job_id: jobId, company, hrefs};
    })()'''
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get("type") != "page":
            continue
        page_url = str(tab.get("url") or "")
        parsed_page = urlsplit(page_url)
        host = (parsed_page.hostname or "").casefold()
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            continue
        if "/jobs/" not in (parsed_page.path or ""):
            continue
        ws_url = str(tab.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            continue
        try:
            payload = _cdp_runtime_evaluate(ws_url, expression)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id.isdigit():
            continue
        bucket = raw.setdefault(job_id, {"companies": set(), "hrefs": set()})
        company = re.sub(r"\s+", " ", str(payload.get("company") or "").strip())[:300]
        if company:
            bucket["companies"].add(company)
        hrefs = payload.get("hrefs")
        if isinstance(hrefs, list):
            for href in hrefs:
                try:
                    canonical = canonical_job_url(str(href or ""))
                except (TypeError, ValueError):
                    continue
                parsed = urlsplit(canonical)
                href_host = (parsed.hostname or "").casefold()
                if canonical and not (href_host == "linkedin.com" or href_host.endswith(".linkedin.com")):
                    bucket["hrefs"].add(canonical)
    verified: dict[str, dict[str, Any]] = {}
    for job_id, bucket in raw.items():
        companies = sorted(bucket["companies"])
        verified[job_id] = {
            "company": companies[0] if len(companies) == 1 else "",
            "apply_hrefs": sorted(bucket["hrefs"]),
        }
    return verified


def _linkedin_ingest_evidence() -> dict[str, Any]:
    evidence = _verified_linkedin_job_evidence()
    return {
        "verified_external_hrefs_by_job": {
            job_id: row.get("apply_hrefs", []) for job_id, row in evidence.items()
        },
        "verified_company_by_job": {
            job_id: str(row.get("company") or "") for job_id, row in evidence.items()
            if str(row.get("company") or "").strip()
        },
    }


def _verified_external_hrefs_by_linkedin_job() -> dict[str, list[str]]:
    """Backward-compatible projection used by older tests/tools."""
    return _linkedin_ingest_evidence()["verified_external_hrefs_by_job"]

def _lease_window_seconds(task: Dict[str, Any]) -> int:
    return max(LEASE_SECONDS, int(task.get("timeout_seconds") or 300) + LEASE_GRACE_SECONDS)


def _lease_heartbeat(task: Dict[str, Any], stop_event: threading.Event) -> None:
    """Extend only this worker's live claim while handler I/O is active."""
    interval = max(10, min(60, _lease_window_seconds(task) // 3))
    while not stop_event.wait(interval):
        try:
            with psycopg.connect(database_dsn(), autocommit=True) as hb_conn, hb_conn.cursor() as hb_cur:
                hb_cur.execute(
                    """UPDATE browser_tasks
                          SET lease_expires_at=now()+make_interval(secs => %s)
                        WHERE id=%s AND status='running' AND locked_by=%s;""",
                    (_lease_window_seconds(task), task["id"], WORKER_ID),
                )
                if hb_cur.rowcount != 1:
                    return
        except Exception as exc:
            print(f"  [Lease] heartbeat warning for {task['id']}: {exc}")


def _cdp_port_from_url() -> int:
    parsed = urlsplit(BROWSER_CDP_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PermanentTaskError(f"Invalid JOBOS browser CDP URL: {BROWSER_CDP_URL!r}")
    if parsed.port is not None:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _current_linkedin_page_url(expected_url: str) -> str:
    """Return the live exact LinkedIn page URL for CAPTCHA binding.

    OpenClaw may add filters and therefore mutate the search query.  CapSolver
    must bind to that live URL, not the seed URL constructed before navigation.
    """
    response = requests.get(BROWSER_CDP_URL.rstrip("/") + "/json", timeout=3)
    response.raise_for_status()
    tabs = response.json()
    if not isinstance(tabs, list):
        raise TransientTaskError("CDP /json did not return a page list while binding LinkedIn CAPTCHA")
    wanted = urlsplit(expected_url)
    wanted_host = (wanted.hostname or "").casefold()
    wanted_path = (wanted.path or "/").rstrip("/") or "/"
    same_page: list[str] = []
    challenge_pages: list[str] = []
    linkedin_pages: list[str] = []
    challenge_markers = ("/checkpoint", "/challenge", "/authwall", "/uas/login", "/login", "/security")
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get("type") != "page":
            continue
        current = str(tab.get("url") or "")
        parsed = urlsplit(current)
        host = (parsed.hostname or "").casefold()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            linkedin_pages.append(current)
            path = (parsed.path or "/").rstrip("/") or "/"
            if any(marker in path.casefold() for marker in challenge_markers):
                challenge_pages.append(current)
            if host == wanted_host and path == wanted_path:
                same_page.append(current)
    # A challenge redirect intentionally changes the path away from /jobs/search.
    # Prefer one explicit challenge/auth page even when another ordinary LinkedIn
    # tab is open; otherwise keep exact same-path affinity before singleton fallback.
    candidates = challenge_pages if len(challenge_pages) == 1 else (same_page or linkedin_pages)
    if len(candidates) != 1:
        raise TransientTaskError(
            f"Cannot bind one live LinkedIn CAPTCHA page for {expected_url!r}; found {len(candidates)} candidates"
        )
    return candidates[0]


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
            "JobOS managed OpenClaw runtime is unavailable; run the managed runtime setup."
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

    parsed = parse_agent_output(proc.stdout)
    if agent == OPENCLAW_AGENT_LINKEDIN_DISCOVERY:
        return blocker_safe_agent_response(parsed)
    return parsed


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
    """Recover only browser tasks whose durable state proves replay is safe.

    Executing tasks with no action journal are pre-I/O. They may be retried only
    while retry budget remains. Once the budget is exhausted the capability is
    closed and the task fails. Any state that may have crossed the browser-I/O
    boundary is terminal until explicit reconciliation.
    """
    # Exhausted executing/no-journal tasks are provably pre-I/O, but their retry
    # budget is gone. Close the exact capability instead of putting them back on
    # the queue once more.
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
    exhausted_rows = cur.fetchall()
    for (task_id,) in exhausted_rows:
        cur.execute("SELECT application_id::text, approval_request_id::text FROM browser_tasks WHERE id=%s;", (task_id,))
        task_row = cur.fetchone()
        if task_row:
            application_id, approval_request_id = task_row
            _expire_bound_pre_io_approval(
                cur, {"id": str(task_id), "application_id": str(application_id or ""),
                      "approval_request_id": str(approval_request_id or "")},
                "Autofill task exhausted before browser I/O.",
            )
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'failed', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": "lease expired; retries exhausted before browser I/O"}), task_id),
        )

    # A crash after durable_begin_execution() but before the first journal row
    # is provably pre-I/O. Release that exact capability only if budget remains.
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
    safe_rows = cur.fetchall()
    for (task_id,) in safe_rows:
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'failed', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": "worker lease expired before first browser action; safe retry"}), task_id),
        )

    # Any state that may have produced external I/O is never replayed.
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
    for (task_id,) in unsafe_rows:
        cur.execute(
            """UPDATE application_attempts
                  SET status = 'needs_review', finished_at = COALESCE(finished_at, now()),
                      detail_json = detail_json || %s
                WHERE browser_task_id = %s AND status = 'started';""",
            (Jsonb({"reason": "worker lease expired after browser execution may have started"}), task_id),
        )

    # Plain not_started tasks are also safe to retry, subject to the same budget.
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
    return len(exhausted_rows) + len(safe_rows) + len(unsafe_rows) + plain_pre_io



def _expire_bound_pre_io_approval(cur, task: Dict[str, Any], reason: str) -> None:
    approval_id = task.get("approval_request_id")
    if not approval_id:
        return
    cur.execute(
        """SELECT EXISTS (SELECT 1 FROM autofill_action_journal WHERE browser_task_id=%s)
            FROM approval_requests
            WHERE id=%s AND application_id=%s
            FOR UPDATE;""",
        (task["id"], approval_id, task.get("application_id")),
    )
    row = cur.fetchone()
    if not row or row[0]:
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
    cur.execute(
        """UPDATE approval_requests
              SET status='expired', executing_task_id=NULL,
                  action_note=COALESCE(action_note,%s)
            WHERE application_id=%s AND type='privileged_upload_document'
              AND parent_approval_request_id=%s::uuid
              AND payload_json->>'delegated_to_autofill'='true'
              AND status IN ('pending','approved','executing')
              AND consumed_at IS NULL;""",
        ("Parent autofill capability closed before browser I/O; delegated child is not executable.",
         task.get("application_id"), str(approval_id)),
    )
    try:
        DEFAULT_PIPELINE_STATE_STORE.transition(
            cur, application_id=str(task.get("application_id") or ""),
            expected_from="awaiting_approval", to="application_form_ready",
            actor=WORKER_ID, reason=reason[:500], detail={"browser_task_id": task["id"]},
            required_kind="recovery",
            guard_sql="""NOT EXISTS (
                SELECT 1 FROM approval_requests ar
                 WHERE ar.application_id=applications.id AND ar.type='autofill_form'
                   AND ar.status IN ('pending','approved','executing')
                   AND ar.token_expires_at > now()
            )""",
        )
    except PipelineStateError:
        pass

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
            RETURNING id, task_type, application_id, approval_request_id, input_json,
                    error_message, retry_count, screenshot_url
        )
        INSERT INTO dead_letter_tasks (
          original_task_id, task_type, application_id,
          input_json, last_error, retry_count, screenshot_url, created_at
        )
        SELECT id, task_type, application_id, input_json,
               error_message, retry_count, screenshot_url, now()
        FROM dead
        RETURNING original_task_id;
        """
    )
    dead_rows = [row[0] for row in cur.fetchall()]
    for task_id in dead_rows:
        # dead_letter_exhausted only selects not_started tasks, so there is no
        # journal and it is safe to retire the bound parent/children together.
        cur.execute("SELECT application_id::text, approval_request_id::text FROM browser_tasks WHERE id=%s;", (task_id,))
        task_row = cur.fetchone()
        if not task_row:
            continue
        application_id, approval_request_id = task_row
        _expire_bound_pre_io_approval(
            cur, {"id": str(task_id), "application_id": str(application_id or ""),
                  "approval_request_id": str(approval_request_id or "")},
            "Browser task dead-lettered before browser I/O.",
        )
    return len(dead_rows)


def claim_next_task(cur) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        UPDATE browser_tasks
        SET status = 'running', locked_by = %s,
            lease_expires_at = now() + make_interval(secs => GREATEST(%s, timeout_seconds + %s)),
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
                  autofill_action_scope, requested_by, idempotency_key;
        """,
        (WORKER_ID, LEASE_SECONDS, LEASE_GRACE_SECONDS),
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
        "requested_by": row[18], "idempotency_key": row[19],
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


def _mark_discovery_scheduler_finished(cur, task: Dict[str, Any]) -> None:
    if task.get("requested_by") != "profile_autonomous_discovery_v1":
        return
    key = str(task.get("idempotency_key") or "")
    state_key = None
    prefix = "jobos:auto-linkedin:"
    if key.startswith(prefix) and not key.startswith("jobos:auto-linkedin-saved"):
        tail = key[len(prefix):]
        fingerprint = tail.split(":", 1)[0]
        if fingerprint:
            state_key = "profile_autonomous_discovery_v1:search:" + fingerprint
    elif key.startswith("jobos:auto-linkedin-saved"):
        state_key = "profile_autonomous_discovery_v1:saved"
    if state_key:
        cur.execute(
            "UPDATE discovery_scheduler_state SET last_finished_at=now(),updated_at=now() WHERE scheduler_key=%s;",
            (state_key,),
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
    """Retry only when durable state proves that no browser write can replay."""
    cur.execute(
        """
        SELECT task_type, execution_state, retry_count, max_retries,
               approval_request_id::text, application_id::text,
               EXISTS (SELECT 1 FROM autofill_action_journal j WHERE j.browser_task_id = b.id)
          FROM browser_tasks b
         WHERE b.id = %s
         FOR UPDATE;
        """,
        (task["id"],),
    )
    row = cur.fetchone()
    if row is None:
        return "failed"

    (task_type, execution_state, retry_count, max_retries,
     approval_request_id, application_id, has_journal) = row
    new_count = (retry_count or 0) + 1
    exhausted = new_count > (max_retries if max_retries is not None else 2)
    durable_task = {
        **task,
        "task_type": task_type,
        "retry_count": retry_count or 0,
        "max_retries": max_retries if max_retries is not None else 2,
        "approval_request_id": approval_request_id,
        "application_id": application_id,
    }

    if task_type == "fill_application_form":

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
                (approval_request_id, application_id, task["id"]),
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
            _expire_bound_pre_io_approval(cur, durable_task, f"Autofill retries exhausted before browser I/O: {error}")

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
               expected_page_fingerprint, bound_autofill_input_hash, bound_autofill_action_scope, payload_json,
               bound_pipeline_version, bound_autofill_plan_key, expected_target_id, bound_autofill_plan_id::text
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
    (approved_id, target_action, bound_doc, bound_hash, bound_origin, artifact_id, artifact_hash, artifact_filename,
     page_url, page_fingerprint_sha, input_hash, action_scope, approval_payload, bound_pipeline_version,
     bound_plan_key, typed_target_id, bound_plan_id) = row
    approval_payload = dict(approval_payload or {})
    cur.execute("SELECT coalesce(job_url,''), coalesce(jd_hash,''), current_step, pipeline_version FROM applications WHERE id=%s;",
                (application_id,))
    app_row = cur.fetchone()
    if (not app_row
            or str(app_row[0] or "") != str(approval_payload.get("application_job_url") or "")
            or str(app_row[1] or "") != str(approval_payload.get("application_jd_hash") or "")
            or str(app_row[2] or "") != str(approval_payload.get("expected_application_step") or "")
            or bound_pipeline_version is None
            or int(app_row[3] or 0) != int(bound_pipeline_version)):
        raise PermanentTaskError("Autofill application job/JD/pipeline version binding changed after approval.")
    expected_target_id = str(typed_target_id or approval_payload.get("expected_target_id") or "").strip()
    if not expected_target_id:
        raise PermanentTaskError("Autofill approval lacks an exact browser target id; prepare a fresh capability.")
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
    if bound_plan_id and task.get("autofill_plan_id") and str(bound_plan_id) != str(task.get("autofill_plan_id")):
        raise PermanentTaskError("Browser task references a different durable autofill plan than its approval.")
    return {"id": approved_id, "document_id": document_id, "expected_origin": origin,
            "artifact_id": artifact_id, "artifact_sha256": artifact_hash, "artifact_filename": artifact_filename,
            "expected_target_id": expected_target_id,
            "expected_initial_url": page_url, "expected_page_fingerprint": page_fingerprint_sha,
            "autofill_input_hash": input_hash, "autofill_action_scope": action_scope or {},
            "autofill_plan_key": str(bound_plan_key or approval_payload.get("autofill_plan_key") or ""),
            "autofill_plan_id": str(bound_plan_id or ""),
            "bound_pipeline_version": bound_pipeline_version,
            "expected_upload_capabilities": approval_payload.get("expected_upload_capabilities") or [],
            "application_job_url": str(approval_payload.get("application_job_url") or ""),
            "application_jd_hash": str(approval_payload.get("application_jd_hash") or ""),
            "expected_application_step": str(approval_payload.get("expected_application_step") or "")}


def require_current_input_hash(binding: Dict[str, Any], current_hash: str) -> None:
    """Reject a capability when profile/legal/document inputs changed post-approval."""
    if str(binding.get("autofill_input_hash") or "") != str(current_hash or ""):
        raise PermanentTaskError(
            "Candidate profile or confirmed legal answer changed after approval; issue a new approval."
        )


def action_is_in_approved_scope(action, scope: Dict[str, Any]) -> bool:
    """Authorize only the exact human-reviewed write tuple.

    Dynamic fields revealed after approval are never covered by a profile-key
    wildcard. Upload identity is part of the parent scope, but browser upload
    still requires a separately approved delegated child capability.
    """
    return action_is_exactly_approved(action, scope)



def _current_upload_document_bindings(cur, application_id: str) -> dict[str, dict[str, str]]:
    cur.execute(
        """SELECT gd.doc_type, gd.id::text, gda.id::text, gda.file_path, gda.filename, gda.sha256,
                  gd.source_jd_hash, a.jd_hash
             FROM applications a
             JOIN generated_document_artifacts gda
               ON gda.id IN (a.approved_resume_artifact_id, a.approved_cover_letter_artifact_id)
             JOIN generated_documents gd ON gd.id = gda.generated_document_id
            WHERE a.id=%s AND gda.application_id=a.id AND gd.application_id=a.id
              AND ((gd.doc_type='resume' AND gd.id=a.approved_resume_id AND gda.id=a.approved_resume_artifact_id)
                OR (gd.doc_type='cover_letter' AND gd.id=a.approved_cover_letter_id AND gda.id=a.approved_cover_letter_artifact_id));""",
        (application_id,),
    )
    return {
        str(kind): {
            "generated_document_id": str(doc_id), "artifact_id": str(artifact_id),
            "file_path": str(path), "filename": str(filename), "sha256": str(sha),
            "source_jd_hash": str(source_jd or ""), "application_jd_hash": str(app_jd or ""),
        }
        for kind, doc_id, artifact_id, path, filename, sha, source_jd, app_jd in cur.fetchall()
    }


def durable_invalidate_delegated_upload(application_id: str, request_id: str, reason: str) -> None:
    """Expire one unusable child before I/O; unrelated safe form fields may continue."""
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE approval_requests
                  SET status='expired', action_note=%s
                WHERE id=%s AND application_id=%s AND type='privileged_upload_document'
                  AND status='approved' AND consumed_at IS NULL
                RETURNING id;""",
            (f"Delegated upload invalidated before browser I/O: {reason}"[:500], request_id, application_id),
        )
        if cur.fetchone() is not None:
            cur.execute(
                """INSERT INTO approval_events(approval_request_id,event,actor,detail_json)
                   VALUES (%s,'expired',%s,%s);""",
                (request_id, WORKER_ID, Jsonb({"reason": reason[:500], "browser_io_started": False})),
            )


def _child_application_binding_matches(cur, application_id: str, payload: dict[str, Any], *,
                                       allowed_steps: set[str] | None = None) -> bool:
    cur.execute("SELECT coalesce(job_url,''), coalesce(jd_hash,''), current_step, pipeline_version FROM applications WHERE id=%s;",
                (application_id,))
    row = cur.fetchone()
    if not row:
        return False
    if (str(payload.get("application_id") or "") != str(application_id)
            or str(payload.get("job_url") or "") != str(row[0] or "")
            or str(payload.get("jd_hash") or "") != str(row[1] or "")):
        return False
    current_step = str(row[2] or "")
    # A delegated upload belongs to the exact parent pipeline incarnation.
    # ``current_step`` alone is vulnerable to ABA (leave a step, later return).
    current_version = int(row[3] if len(row) > 3 and row[3] is not None else payload.get("expected_pipeline_version") or 0)
    expected_version = payload.get("expected_pipeline_version")
    if expected_version is None or int(expected_version) != current_version:
        return False
    if allowed_steps is not None:
        return current_step in allowed_steps
    return str(payload.get("expected_application_step") or "") == current_step


def load_delegated_upload_capabilities(cur, task: Dict[str, Any], binding: Dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Load only live upload children for this exact parent plan.

    Invalid/stale children are expired before any browser I/O and omitted, so
    the planner degrades that upload to a pause instead of aborting safe writes.
    """
    plan_key = str(binding.get("autofill_plan_key") or "")
    if not plan_key:
        return {}
    expected = binding.get("expected_upload_capabilities")
    expected = expected if isinstance(expected, list) else []
    expected_identities = {
        tuple(str(item.get(key) or "") for key in ("field_ref", "document_type", "artifact_id", "sha256"))
        for item in expected if isinstance(item, dict)
    }
    cur.execute(
        """SELECT id::text, payload_json
             FROM approval_requests
            WHERE application_id=%s AND type='privileged_upload_document'
              AND status='approved' AND consumed_at IS NULL AND token_expires_at > now()
              AND bound_autofill_plan_key=%s
              AND parent_approval_request_id=%s::uuid
              AND payload_json->>'delegated_to_autofill'='true'
            ORDER BY created_at;""",
        (task["application_id"], plan_key, str(binding.get("id") or "")),
    )
    current_documents = _current_upload_document_bindings(cur, task["application_id"])
    result: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for request_id, raw_payload in cur.fetchall():
        request_id, payload = str(request_id), dict(raw_payload or {})
        ident = tuple(str(payload.get(key) or "") for key in ("field_ref", "document_type", "artifact_id", "sha256"))
        invalid = ""
        if str(payload.get("parent_approval_request_id") or "") != str(binding.get("id") or ""):
            invalid = "Upload child belongs to a different parent autofill approval."
        elif ident not in expected_identities:
            invalid = "Upload child is not an expected identity of the approved parent plan."
        elif not _child_application_binding_matches(
            cur, task["application_id"], payload, allowed_steps={"awaiting_approval", "autofill_executing"}
        ):
            invalid = "Upload child application/JD/pipeline binding changed."
        try:
            same_url = canonical_page_url(str(payload.get("expected_url") or "")) == canonical_page_url(str(binding.get("expected_initial_url") or ""))
        except ValueError:
            same_url = False
        if not invalid and (not same_url
                or str(payload.get("expected_origin") or "") != str(binding.get("expected_origin") or "")
                or str(payload.get("expected_page_fingerprint") or "") != str(binding.get("expected_page_fingerprint") or "")):
            invalid = "Upload child page binding changed."
        doc_type = str(payload.get("document_type") or "")
        approved_doc = {key: str(payload.get(key) or "") for key in (
            "generated_document_id", "artifact_id", "file_path", "filename", "sha256",
            "source_jd_hash", "application_jd_hash",
        )}
        if not invalid and (current_documents.get(doc_type) != approved_doc
                or not approved_doc["source_jd_hash"]
                or approved_doc["source_jd_hash"] != approved_doc["application_jd_hash"]):
            invalid = "Upload child document pointer/JD binding changed."
        path: Path | None = None
        if not invalid:
            try:
                path = Path(approved_doc["file_path"]).expanduser().resolve()
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != approved_doc["sha256"].casefold():
                    invalid = "Upload child artifact bytes changed or are unavailable."
            except (OSError, RuntimeError, ValueError):
                invalid = "Upload child artifact bytes changed or are unavailable."
        ref = str(payload.get("field_ref") or "")
        if not invalid and not ref:
            invalid = "Upload child field binding is missing."
        if invalid:
            durable_invalidate_delegated_upload(task["application_id"], request_id, invalid)
            continue
        if ref in ambiguous:
            durable_invalidate_delegated_upload(task["application_id"], request_id, "Duplicate delegated upload field is ambiguous.")
            continue
        if ref in result:
            prior = result.pop(ref)
            ambiguous.add(ref)
            durable_invalidate_delegated_upload(task["application_id"], str(prior["id"]), "Duplicate delegated upload field is ambiguous.")
            durable_invalidate_delegated_upload(task["application_id"], request_id, "Duplicate delegated upload field is ambiguous.")
            continue
        result[ref] = {"id": request_id, "payload": payload, "resolved_path": str(path)}
    return result


def upload_capability_matches_action(action, child: dict[str, Any], scope: Dict[str, Any]) -> bool:
    payload = child.get("payload") if isinstance(child, dict) else None
    if not isinstance(payload, dict) or not action_is_in_approved_scope(action, scope):
        return False
    doc_type = str(getattr(action, "profile_key", "") or "").removeprefix("documents.")
    try:
        action_path = str(Path(str(getattr(action, "value", "") or "")).expanduser().resolve())
    except Exception:
        return False
    return (str(payload.get("field_ref") or "") == str(getattr(action, "ref", "") or "")
            and str(payload.get("document_type") or "") == doc_type
            and str(child.get("resolved_path") or "") == action_path)


def _durable_connection():
    """A commit independent of the worker's task transaction.

    Browser writes cannot roll back.  These small state changes therefore
    deliberately survive a later rollback of the main queue transaction.
    """
    # A dedicated connection is durable relative to the queue worker's outer
    # transaction, but its related statements must still commit atomically.
    return psycopg.connect(database_dsn(), autocommit=False)


def durable_begin_execution(task: Dict[str, Any], binding: Dict[str, Any], target_id: str) -> None:
    """Acquire the application/browser/capability fence in one durable CAS before I/O."""
    with _durable_connection() as conn, conn.cursor() as cur:
        expected_step = str(binding.get("expected_application_step") or "awaiting_approval")
        if expected_step != "awaiting_approval":
            raise PermanentTaskError("Autofill capability is not bound to awaiting_approval.")
        try:
            DEFAULT_PIPELINE_STATE_STORE.transition(
                cur, application_id=task["application_id"], expected_from=expected_step,
                to="autofill_executing", actor=WORKER_ID,
                reason="Acquired durable application execution fence immediately before deterministic browser I/O.",
                detail={"browser_task_id": task["id"], "target_id": target_id},
                required_kind="automated",
                expected_job_url=str(binding.get("application_job_url") or ""),
                expected_jd_hash=str(binding.get("application_jd_hash") or ""),
                allow_already_target=False,
            )
        except PipelineStateError as exc:
            raise PermanentTaskError("Application lifecycle changed before browser I/O; refusing the write.") from exc
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



def durable_begin_delegated_upload(task: Dict[str, Any], child: dict[str, Any], target_id: str) -> str:
    payload = dict(child.get("payload") or {})
    if str(payload.get("target_id") or "") != str(target_id):
        raise PermanentTaskError("Upload child target changed after approval.")
    with _durable_connection() as conn, conn.cursor() as cur:
        if str(payload.get("parent_approval_request_id") or "") != str(task.get("approval_request_id") or ""):
            raise PermanentTaskError("Upload child belongs to a different parent autofill approval.")
        if not _child_application_binding_matches(
            cur, task["application_id"], payload, allowed_steps={"autofill_executing"}
        ):
            raise PermanentTaskError("Upload child application/JD/pipeline binding changed before I/O.")
        cur.execute(
            """UPDATE approval_requests SET status='executing', executing_task_id=%s
                  WHERE id=%s AND application_id=%s AND type='privileged_upload_document'
                    AND status='approved' AND consumed_at IS NULL AND token_expires_at > now()
                  RETURNING id;""",
            (task["id"], child["id"], task["application_id"]),
        )
        if cur.fetchone() is None:
            raise PermanentTaskError("Upload child approval changed before I/O; prepare a fresh form plan.")
        cur.execute(
            """INSERT INTO privileged_action_executions(
                   approval_request_id, application_id, action_type, status, target_id,
                   expected_url, expected_page_fingerprint)
               VALUES (%s,%s,'privileged_upload_document','running',%s,%s,%s)
               ON CONFLICT (approval_request_id) DO NOTHING RETURNING id::text;""",
            (child["id"], task["application_id"], target_id,
             payload.get("expected_url"), payload.get("expected_page_fingerprint")),
        )
        row = cur.fetchone()
        if not row:
            raise PermanentTaskError("Upload child already has an execution record; never replay it.")
        return str(row[0])


def durable_complete_delegated_upload(task: Dict[str, Any], child: dict[str, Any], execution_id: str, target_id: str) -> None:
    payload = dict(child.get("payload") or {})
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE privileged_action_executions
                  SET status='completed', observed_url=expected_url,
                      observed_page_fingerprint=expected_page_fingerprint,
                      result_json=%s, finished_at=now()
                WHERE id=%s AND approval_request_id=%s AND status='running';""",
            (Jsonb({"uploaded": True, "target_id": target_id, "field_ref": payload.get("field_ref"),
                    "artifact_id": payload.get("artifact_id"), "filename": payload.get("filename")}),
             execution_id, child["id"]),
        )
        if cur.rowcount != 1:
            raise PermanentTaskError("Upload execution record changed unexpectedly; reconciliation is required.")
        cur.execute(
            """UPDATE approval_requests
                  SET status='consumed', consumed_at=now(), consumed_by=%s, executing_task_id=NULL
                WHERE id=%s AND application_id=%s AND status='executing' AND executing_task_id=%s
                RETURNING id;""",
            (WORKER_ID, child["id"], task["application_id"], task["id"]),
        )
        if cur.fetchone() is None:
            raise PermanentTaskError("Upload child capability changed after verified I/O; reconciliation is required.")


def durable_reconcile_delegated_upload(task: Dict[str, Any], child: dict[str, Any], execution_id: str,
                                       target_id: str | None, reason: str) -> None:
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE privileged_action_executions
                  SET status='needs_reconciliation', error_message=%s, finished_at=now()
                WHERE id=%s AND approval_request_id=%s AND status='running';""",
            (reason[:2000], execution_id, child["id"]),
        )
        cur.execute(
            """UPDATE approval_requests
                  SET status='consumed', consumed_at=now(), consumed_by=%s,
                      executing_task_id=NULL, action_note=%s
                WHERE id=%s AND application_id=%s AND status='executing';""",
            (WORKER_ID, f"Uncertain delegated upload: {reason}"[:500],
             child["id"], task["application_id"]),
        )
        cur.execute(
            """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
               VALUES (%s,'privileged_action_needs_reconciliation',%s,%s);""",
            (task["application_id"], WORKER_ID,
             Jsonb({"approval_request_id": child["id"], "action_type": "privileged_upload_document",
                    "target_id": target_id, "reason": reason[:500]})),
        )


def durable_close_unused_upload_capabilities(task: Dict[str, Any], capabilities: dict[str, dict[str, Any]]) -> None:
    ids = [str(item.get("id") or "") for item in capabilities.values() if item.get("id")]
    if not ids:
        return
    with _durable_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE approval_requests SET status='expired', action_note=COALESCE(action_note,%s)
                 WHERE id = ANY(%s) AND application_id=%s AND type='privileged_upload_document'
                   AND status='approved' AND consumed_at IS NULL;""",
            ("Parent autofill session closed without consuming this exact upload capability.",
             ids, task["application_id"]),
        )


def durable_finish_execution(task: Dict[str, Any], binding: Dict[str, Any], result) -> None:
    """Durably finish only if the application execution fence still belongs to this run.

    A post-I/O lifecycle race must never be reported as completed. If the
    application moved away from autofill_executing, preserve all journals and
    retire the one-shot capability as non-replayable reconciliation work.
    """
    execution_state = "completed" if result.status == "completed" else "partial"
    reconcile_reason: str | None = None
    with _durable_connection() as conn, conn.cursor() as cur:
        if execution_state == "completed":
            try:
                DEFAULT_PIPELINE_STATE_STORE.transition(
                    cur, application_id=task["application_id"], expected_from="autofill_executing",
                    to="form_filled", actor=WORKER_ID,
                    reason="Deterministic autofill completed under the application execution fence.",
                    detail={"browser_task_id": task["id"]},
                    required_kind="automated",
                    expected_job_url=str(binding.get("application_job_url") or ""),
                    expected_jd_hash=str(binding.get("application_jd_hash") or ""),
                    allow_already_target=False,
                )
            except PipelineStateError:
                reconcile_reason = (
                    "Application lifecycle changed after deterministic browser I/O; "
                    "browser effects require reconciliation and are not replayable."
                )
        else:
            # Partial progress crossed the browser-I/O boundary. If the
            # application fence was lost concurrently, preserve the same
            # reconciliation invariant as the completed path; never leave a
            # partially-written task looking retryable.
            try:
                DEFAULT_PIPELINE_STATE_STORE.transition(
                    cur, application_id=task["application_id"], expected_from="autofill_executing",
                    to="awaiting_approval", actor=WORKER_ID,
                    reason="Deterministic autofill ended partial; old capability is terminal and a fresh plan is required.",
                    detail={"browser_task_id": task["id"]},
                    required_kind="recovery",
                    expected_job_url=str(binding.get("application_job_url") or ""),
                    expected_jd_hash=str(binding.get("application_jd_hash") or ""),
                    allow_already_target=False,
                )
            except PipelineStateError:
                reconcile_reason = (
                    "Application lifecycle changed after partial deterministic browser I/O; "
                    "browser effects require reconciliation and are not replayable."
                )

        terminal_task_state = "needs_reconciliation" if reconcile_reason else execution_state
        cur.execute(
            """UPDATE approval_requests
                SET status='consumed', consumed_at=now(), consumed_by=%s,
                    executing_task_id = NULL, action_note=COALESCE(action_note,%s)
              WHERE id=%s AND application_id=%s
                AND status='executing' AND executing_task_id=%s
              RETURNING id;""",
            (WORKER_ID, reconcile_reason, binding["id"], task["application_id"], task["id"]),
        )
        if cur.fetchone() is None:
            raise PermanentTaskError("Executing approval changed unexpectedly; manual reconciliation is required.")
        cur.execute(
            """UPDATE browser_tasks
                  SET execution_state=%s, pinned_target_id=%s,
                      error_message=COALESCE(%s::text, error_message)
                WHERE id=%s;""",
            (terminal_task_state, result.target_id, reconcile_reason, task["id"]),
        )
        cur.execute(
            """UPDATE application_attempts
                  SET status=%s, finished_at=now(), detail_json=detail_json || %s
                WHERE browser_task_id=%s AND status='started';""",
            ("needs_review" if reconcile_reason else execution_state,
             Jsonb({"verified_refs": list(result.verified_refs),
                    "failed_refs": list(result.failed_refs),
                    "reconciliation_reason": reconcile_reason}), task["id"]),
        )
        if reconcile_reason:
            cur.execute(
                """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                   VALUES (%s,'autofill_needs_reconciliation',%s,%s);""",
                (task["application_id"], WORKER_ID,
                 Jsonb({"browser_task_id": task["id"], "reason": reconcile_reason})),
            )
    if reconcile_reason:
        raise PermanentTaskError(reconcile_reason)


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
        plan_key = str(binding.get("autofill_plan_key") or "")
        parent_request_id = str(binding.get("id") or "")
        if plan_key and parent_request_id:
            cur.execute(
                """UPDATE approval_requests SET status='expired', executing_task_id=NULL,
                           action_note=COALESCE(action_note,%s)
                     WHERE application_id=%s AND type='privileged_upload_document'
                       AND parent_approval_request_id=%s::uuid
                       AND payload_json->>'delegated_to_autofill'='true'
                       AND status IN ('pending','approved');""",
                ("Parent autofill closed before browser I/O; delegated child is no longer executable.",
                 task["application_id"], parent_request_id),
            )
        try:
            DEFAULT_PIPELINE_STATE_STORE.transition(
                cur, application_id=task["application_id"], expected_from="awaiting_approval",
                to="application_form_ready", actor=WORKER_ID, reason=reason[:500],
                detail={"browser_task_id": task["id"]}, required_kind="recovery",
                guard_sql="""NOT EXISTS (
                    SELECT 1 FROM approval_requests ar
                     WHERE ar.application_id=applications.id AND ar.type='autofill_form'
                       AND ar.status IN ('pending','approved','executing')
                       AND ar.token_expires_at > now()
                )""",
            )
        except PipelineStateError:
            pass


def load_allowed_domains(cur) -> list:
    cur.execute("SELECT domain FROM allowed_domains WHERE enabled = true;")
    return [r[0].lower() for r in cur.fetchall()]


def check_domain(cur, url: str, *, application_id: str | None = None,
                 purpose: str | None = None) -> None:
    """The domain whitelist existed in the schema (allowed_domains,
    migration 038) and was enforced inside autofill_agent_v1.py, but this
    worker -- the actual single chokepoint where every browser task
    (including ones NOT run through autofill_agent_v1.py) reaches
    OpenClaw -- never called it. A JD or company page containing a link
    could send the agent anywhere. Fixed 2026-07-31: enforced here too,
    same logic as autofill_agent_v1.py's check_domain."""
    from services.common.domain_authority_v1 import host_is_authorized
    m = re.search(r"https?://([^/]+)", url)
    host = (m.group(1) if m else "").lower().split(":")[0]
    if host_is_authorized(cur, url, application_id=application_id,
                          purpose=purpose or "employer_handoff"):
        return
    raise PermanentTaskError(
        f"Domain '{host}' is not globally authorized and has no live application-scoped trust. "
        "Use the privileged trust-domain approval for this application; do not promote ATS catalog domains to global authority."
    )


def require_url(cur, input_json: Dict[str, Any]) -> str:
    """Require an HTTP(S) URL and enforce the central allowed-domain gate."""
    raw_url = input_json.get("url")
    url = raw_url.strip() if isinstance(raw_url, str) else ""
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
                          supports_select, supports_upload, verification_level
                   FROM ats_capabilities WHERE ats_type = %s;""", (ats_type,))
    capability = cur.fetchone()
    if not capability or capability[0] == "review_only" or capability[5] == "review_only":
        raise PermanentTaskError(
            f"ATS '{ats_type}' is review-only or unregistered. Browser writes require an explicit capability row."
        )
    verification = str(capability[5])
    if verification not in {"generic_accessibility", "fixture_verified", "live_verified"}:
        raise PermanentTaskError(f"ATS '{ats_type}' has an invalid browser verification level: {verification!r}.")
    # generic_accessibility authorizes only the same four deterministic
    # primitives emitted by the form inspector/planner. It is deliberately
    # not a claim that vendor-specific custom widgets/selectors are proven.
    return {"fill": bool(capability[1]), "check": bool(capability[2]),
            "select": bool(capability[3]), "upload": bool(capability[4])}


def snapshot_state(transport: OpenClawTransport, target_id: str) -> SnapshotState:
    payload = transport.snapshot(target_id)
    nodes = parse_snapshot(payload)
    return SnapshotState(
        tuple(inspect_nodes(nodes)), tuple(inspect_question_groups(nodes)),
        page_fingerprint(payload, page_url=transport.current_url(target_id)), coerce_bool(payload.get("truncated")),
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
    authorized = inp.get("user_initiated") is True or (
        inp.get("autonomous_discovery") is True
        and task.get("requested_by") == "profile_autonomous_discovery_v1"
    )
    if not authorized:
        raise PermanentTaskError(
            "LinkedIn discovery requires explicit user initiation or the bounded profile discovery planner."
        )
    try:
        request = validate_search_request(
            str(inp.get("keywords") or ""), str(inp.get("location") or ""), inp.get("max_results"),
            max_allowed=(MAX_AUTONOMOUS_DISCOVERY_RESULTS if inp.get("autonomous_discovery") is True else MAX_DISCOVERY_RESULTS),
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
        "click any Apply button, or navigate to an external apply site. Report an external "
        "apply_url only when that exact URL is already visible/grounded in the current snapshot. "
        "If the existing browser session is not signed in or a "
        "CAPTCHA appears, stop and report that exact blocker.\n\n"
        "Open each included job detail in its own tab and leave those detail tabs open until after the final JSON so JobOS can independently verify company/apply evidence. "
        "Open at most the requested number of eligible result details. Snapshot each detail pane "
        "and copy its complete visible `About the job` text. "
        "Use a canonical URL exactly like https://www.linkedin.com/jobs/view/<numeric-id>/; "
        "discard tracking query parameters.\n\n"
        "For each read result return ONLY one JSON object in this exact form:\n"
        "{\"jobs\":[{\"company\":\"...\",\"title\":\"...\",\"location\":\"...\","
        "\"work_mode\":\"remote|hybrid|on-site|unknown\",\"url\":\"https://www.linkedin.com/jobs/...\","
        "\"apply_url\":\"exact external employer/ATS apply URL if visibly grounded, otherwise empty\","
        "\"salary_range\":\"published compensation text if visible, otherwise empty\","
        "\"jd_text\":\"full visible job description text\"}]}\n"
        "Include only pages with a full visible JD of at least 200 characters. Do not "
        "summarise, infer, invent a URL, or include jobs beyond the cap. If extraction "
        "cannot be grounded in the snapshot, return {\"jobs\":[]} rather than guessing."
    )

    agent_response = _run_linkedin_agent_with_fake_mouse(
        message=msg, timeout=task["timeout_seconds"], session_id=f"jobos-task-{task['id']}"
    )

    # =====================================================================
    # 2. CHECK CAPTCHA & PARALLEL BYPASS (CHỮA BỆNH VÀ PERMANENT FAILURE)
    # =====================================================================
    blocker_kind = classify_linkedin_blocker(agent_response)
    # Expired sessions/authwalls require the human to re-authenticate. Do not
    # send an auth problem to CapSolver or mark it permanently impossible.
    if blocker_kind == "auth":
        raise TransientTaskError(
            "LinkedIn session requires manual re-authentication; task is retryable after login."
        )
    if blocker_kind == "captcha":
        print("  [Bypass] Đụng độ CAPTCHA/challenge LinkedIn! Kích hoạt CapSolver...")
        try:
            cdp_port = _cdp_port_from_url()
            live_captcha_url = _current_linkedin_page_url(search_url)

            # Bind CapSolver to the exact live page after LinkedIn applied filters.
            execute_parallel_bypass(
                cdp_port=cdp_port,
                website_url=live_captcha_url,
                website_key="2CB16598-CB82-458A-898B-53544380C934", # FunCaptcha public key mặc định của LinkedIn
                regimes_path="data/pointer-regimes.json",
                captcha_type="FunCaptchaTaskProxyless"
            )
            print("  [Bypass] Vượt rào thành công! Thử cào lại lần 2...")
            
            # Every retry gets a new FakeMouse lifecycle rather than reusing a
            # stopped thread from the first browser attempt.
            agent_response = _run_linkedin_agent_with_fake_mouse(
                message=msg, timeout=task["timeout_seconds"],
                session_id=f"jobos-task-{task['id']}-after-captcha",
            )
            
            # Nếu lần 2 vẫn dính -> Buông súng, đánh dấu Permanent Failure
            retry_blocker = classify_linkedin_blocker(agent_response)
            if retry_blocker == "auth":
                raise TransientTaskError("LinkedIn session expired after CAPTCHA solve; re-authenticate and retry.")
            if retry_blocker == "captcha":
                raise PermanentTaskError("CapSolver đã giải nhưng vẫn bị chặn. Đánh dấu lỗi vĩnh viễn (Permanent Failure)!")
        except PermanentTaskError:
            raise
        except Exception as e:
            # Network/CDP/OpenClaw/CapSolver availability is transient. Only a
            # second grounded blocker after a successful bypass is permanent.
            raise TransientTaskError(f"Bypass tạm thời thất bại hoặc không thể kết nối: {e}") from e

    # =====================================================================
    # 3. LƯU VÀO DB
    # =====================================================================
    try:
        ingest_input = {**inp, **_linkedin_ingest_evidence()}
        intake = ingest_discovered_jobs(cur, task["id"], ingest_input, agent_response)
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(f"LinkedIn discovery result refused: {exc}") from exc

    return {
        "search_url": search_url, "search": request, "submitted": False,
        "auto_ingest": intake, "agent_response": agent_response,
    }

def handle_discover_linkedin_saved_jobs(cur, task) -> Dict[str, Any]:
    """Read only the jobs the user has already saved on LinkedIn.

    This is deliberately a separate handler: it does not modify the existing
    LinkedIn discovery, parallel bypass, CAPTCHA detection, or CapSolver flow.
    """
    if not feature_enabled("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED"):
        raise PermanentTaskError("LinkedIn Saved Jobs discovery is disabled by configuration.")
    inp = task["input_json"]
    authorized = inp.get("user_initiated") is True or (
        inp.get("autonomous_discovery") is True
        and task.get("requested_by") == "profile_autonomous_discovery_v1"
    )
    if not authorized:
        raise PermanentTaskError(
            "Saved Jobs sync requires explicit user initiation or the bounded profile discovery planner."
        )
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
        f"Read at most {request['max_results']} existing saved job postings and their complete visible job descriptions.\n"
        "Open each included saved job detail in its own tab and leave those tabs open until the final JSON so JobOS can independently verify company/apply evidence.\n\n"
        "Never authenticate, save/unsave, apply, message, upload, fill fields, submit, click any Apply button, "
        "or navigate to an external apply site. Report apply_url only if the exact external URL is already visible "
        "in the current snapshot. If login or a checkpoint appears, stop and report it.\n"
        "Return ONLY JSON: {\"jobs\":[{\"company\":\"...\",\"title\":\"...\",\"location\":\"...\","
        "\"work_mode\":\"remote|hybrid|on-site|unknown\",\"url\":\"https://www.linkedin.com/jobs/view/<numeric-id>/\","
        "\"apply_url\":\"exact external employer/ATS apply URL if visibly grounded, otherwise empty\","
        "\"jd_text\":\"full visible job description\"}]}. "
        "Each record needs a grounded canonical URL and 200+ JD characters."
    )

    agent_response = _run_linkedin_agent_with_fake_mouse(
        message=msg, timeout=task["timeout_seconds"], session_id=f"jobos-saved-{task['id']}"
    )

    saved_blocker = classify_linkedin_blocker(agent_response)
    if saved_blocker == "auth":
        raise TransientTaskError("LinkedIn Saved Jobs session requires manual re-authentication; retry after login.")
    if saved_blocker == "captcha":
        # Saved Jobs is read-only and uses the same exact-page CAPTCHA recovery
        # boundary as normal LinkedIn search.  Treat network/CDP/CapSolver
        # availability as transient; only a second grounded CAPTCHA is terminal.
        try:
            cdp_port = _cdp_port_from_url()
            live_captcha_url = _current_linkedin_page_url(saved_url)
            execute_parallel_bypass(
                cdp_port=cdp_port,
                website_url=live_captcha_url,
                website_key="2CB16598-CB82-458A-898B-53544380C934",
                regimes_path="data/pointer-regimes.json",
                captcha_type="FunCaptchaTaskProxyless",
            )
            agent_response = _run_linkedin_agent_with_fake_mouse(
                message=msg, timeout=task["timeout_seconds"],
                session_id=f"jobos-saved-{task['id']}-after-captcha",
            )
            retry_blocker = classify_linkedin_blocker(agent_response)
            if retry_blocker == "auth":
                raise TransientTaskError(
                    "LinkedIn Saved Jobs session expired after CAPTCHA solve; re-authenticate and retry."
                )
            if retry_blocker == "captcha":
                raise PermanentTaskError(
                    "CapSolver completed but LinkedIn Saved Jobs remains on a grounded CAPTCHA/checkpoint."
                )
        except (PermanentTaskError, TransientTaskError):
            raise
        except Exception as exc:
            raise TransientTaskError(
                f"LinkedIn Saved Jobs CAPTCHA bypass temporarily failed: {exc}"
            ) from exc
    try:
        ingest_input = {**inp, **_linkedin_ingest_evidence()}
        intake = ingest_saved_jobs(cur, task["id"], ingest_input, agent_response)
    except LinkedInDiscoveryError as exc:
        raise PermanentTaskError(f"LinkedIn Saved Jobs result refused: {exc}") from exc
    return {"saved_url": saved_url, "saved": request, "submitted": False,
            "auto_ingest": intake, "agent_response": agent_response}


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
    upload_capabilities = load_delegated_upload_capabilities(cur, task, binding)
    approved_upload_hashes = {
        str(item["resolved_path"]): str((item.get("payload") or {}).get("sha256") or "")
        for item in upload_capabilities.values()
    }
    transport = OpenClawTransport(
        binary=OPENCLAW_BIN, profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"),
        timeout=min(int(task["timeout_seconds"]), 90), environment=openclaw_runtime_env(),
        approved_upload_hashes=approved_upload_hashes,
    )
    execution_started = False
    pinned_target_id: str | None = None
    latest_actions = []
    active_upload: tuple[dict[str, Any], str] | None = None

    # Deterministic autofill intentionally does not run synthetic mouse motion.
    # Hover-driven DOM changes would undermine exact page/ref verification.

    def make_plan(state: SnapshotState):
        nonlocal latest_actions
        latest_actions, _ = plan_autofill(
            list(state.fields), context.profile, question_groups=list(state.groups),
            approved_sensitive_answers=context.sensitive_answers,
            remembered_answers=context.remembered_answers,
        )
        approved_actions = []
        for action in latest_actions:
            if action.action == "upload":
                child = upload_capabilities.get(str(action.ref))
                in_scope = bool(child and upload_capability_matches_action(
                    action, child, binding["autofill_action_scope"]
                ))
                refusal = "Document upload needs its exact separately approved upload capability."
            else:
                in_scope = action_is_in_approved_scope(action, binding["autofill_action_scope"])
                refusal = "Action is outside the exact human-reviewed scope."
            if in_scope and (action.action not in action_capabilities or action_capabilities[action.action]):
                approved_actions.append(action)
            else:
                approved_actions.append(type(action)(
                    "pause", action.ref, None, action.profile_key,
                    refusal if not in_scope else f"ATS capability does not permit {action.action}.",
                    action.question_label,
                ))
        latest_actions = approved_actions
        return latest_actions

    def begin_execution(target_id: str) -> None:
        nonlocal execution_started, pinned_target_id
        durable_begin_execution(task, binding, target_id)
        execution_started, pinned_target_id = True, target_id

    def before_io(action, target_id: str, journal_id: str) -> None:
        nonlocal active_upload
        if action.action != "upload":
            return
        child = upload_capabilities.get(str(action.ref))
        if not child or not upload_capability_matches_action(action, child, binding["autofill_action_scope"]):
            raise PermanentTaskError("Exact separately approved upload capability is unavailable.")
        execution_id = durable_begin_delegated_upload(task, child, target_id)
        active_upload = (child, execution_id)

    def after_verified(action, target_id: str, journal_id: str) -> None:
        nonlocal active_upload
        durable_journal_verified(action, target_id, journal_id)
        if action.action == "upload":
            if active_upload is None:
                raise PermanentTaskError("Verified upload has no active one-shot child capability.")
            child, execution_id = active_upload
            durable_complete_delegated_upload(task, child, execution_id, target_id)
            active_upload = None

    def after_failed(action, target_id: str, journal_id: str) -> None:
        nonlocal active_upload
        durable_journal_failed(action, target_id, journal_id)
        if action.action == "upload" and active_upload is not None:
            child, execution_id = active_upload
            active_upload = None
            durable_reconcile_delegated_upload(
                task, child, execution_id, target_id,
                "Upload I/O occurred but the exact filename/field effect could not be verified.",
            )
            raise PermanentTaskError(
                "Delegated upload entered needs_reconciliation; parent autofill stops immediately and cannot continue writes."
            )

    try:
        session = AutofillSession(
            transport=transport, expected_origin=binding["expected_origin"],
            expected_target_id=binding["expected_target_id"],
            expected_initial_url=binding["expected_initial_url"],
            expected_page_fingerprint=binding["expected_page_fingerprint"],
            snapshot_state=lambda target_id: snapshot_state(transport, target_id),
            origin_allowed=lambda url: check_domain(cur, url, application_id=task["application_id"]),
            begin_execution=begin_execution,
            before_action=lambda action, target_id: durable_journal_start(task, binding, action, target_id),
            before_io=before_io,
            after_verified=after_verified,
            after_failed=after_failed,
        )
        result = session.execute(make_plan)
        durable_close_unused_upload_capabilities(task, upload_capabilities)
        if execution_started:
            durable_finish_execution(task, binding, result)
        else:
            durable_close_unstarted_approval(
                task, binding,
                "No deterministic browser write ran; issue a fresh approval after review or form changes.",
            )
    except (SessionError, TransportError, PermanentTaskError) as exc:
        if active_upload is not None:
            child, execution_id = active_upload
            active_upload = None
            durable_reconcile_delegated_upload(task, child, execution_id, pinned_target_id, str(exc))
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
        # the deterministic browser write transaction. Even an unexpected
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
        "paused_fields": [{"question": action.question_label or "", "reason": action.reason,
                           "profile_key": action.profile_key}
                          for action in latest_actions if action.action == "pause"],
        "approval_consumed": execution_started,
        "approval_closed_without_write": not execution_started,
        "screenshot_path": screenshot_path,
        "submitted": False, }

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
            agent=OPENCLAW_AGENT_BROWSE,
            message=msg,
            timeout=task["timeout_seconds"],
            session_id=f"jobos-task-{task['id']}",
        ),
    }


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

    lease_stop = threading.Event()
    lease_thread = threading.Thread(
        target=_lease_heartbeat, args=(task, lease_stop),
        name=f"jobos-lease-{task['id']}", daemon=True,
    )
    lease_thread.start()

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
            _mark_discovery_scheduler_finished(cur, task)
        # Browser completion is a durable boundary of its own. A failure in
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
                # review later. Never replay/reconcile browser writes because
                # the review/UI materialization layer failed.
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
            if task["task_type"] == "fill_application_form":
                # Preflight failures happen before handle_fill_application_form
                # can enter its local cleanup guard.  Close only if the durable
                # journal proves that no browser write crossed the boundary.
                _expire_bound_pre_io_approval(cur, task, f"Preflight refused: {e}")
            fail_task(cur, task["id"], str(e))
            _mark_discovery_scheduler_finished(cur, task)
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
            if status == "failed":
                _mark_discovery_scheduler_finished(cur, task)
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
            if status == "failed":
                _mark_discovery_scheduler_finished(cur, task)
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
    finally:
        lease_stop.set()
        lease_thread.join(timeout=2)

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

    with psycopg.connect(database_dsn(), autocommit=False) as conn:
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
