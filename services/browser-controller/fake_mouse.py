"""Public compatibility entrypoint for the retained FakeMouse feature.

The motion implementation lives in ``services.autofill.parallel_bypass``.  This
module is the public caller seam used by LinkedIn discovery so browser workers
do not import a private implementation function directly.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from services.autofill.parallel_bypass import _fake_mouse_routine, _is_linkedin_url
from services.common.config import database_dsn

log = logging.getLogger(__name__)


def _seed_websocket(cdp_url: str = "http://127.0.0.1:9222/json", *, wait_seconds: float = 5.0) -> str:
    """Boundedly wait through Chrome's transient empty-target startup race."""
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    detail = "CDP returned no page targets"
    while True:
        try:
            response = requests.get(cdp_url, timeout=3)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError("CDP /json did not return a page list")
            tabs = [t for t in payload if isinstance(t, dict) and t.get("type") == "page"
                    and t.get("webSocketDebuggerUrl")]
            linkedin = [t for t in tabs if _is_linkedin_url(str(t.get("url") or ""))]
            if linkedin:
                # The seed only locates the catalog. The retained controller
                # then enrolls every current/future LinkedIn page tab.
                return str(linkedin[0]["webSocketDebuggerUrl"])
            if len(tabs) == 1:
                return str(tabs[0]["webSocketDebuggerUrl"])
            detail = f"legacy FakeMouse requires one exact non-LinkedIn page; found {len(tabs)}"
        except Exception as exc:
            detail = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError(detail)
        time.sleep(0.25)


def _deterministic_browser_io_active() -> bool:
    """Durable cross-process fence; uncertainty stops compatibility motion."""
    try:
        import psycopg
        with psycopg.connect(database_dsn(), connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT
                     EXISTS (SELECT 1 FROM browser_tasks WHERE execution_state='executing')
                     OR EXISTS (SELECT 1 FROM privileged_action_executions WHERE status='running');"""
            )
            return bool(cur.fetchone()[0])
    except Exception as exc:
        log.warning("[FakeMouse] execution fence unavailable; stopping safely: %s", exc)
        return True


def _watch_execution_fence(stop_event: threading.Event) -> None:
    while not stop_event.wait(0.25):
        if _deterministic_browser_io_active():
            log.info("[FakeMouse] deterministic browser I/O active; stopping all motion")
            stop_event.set()
            return


def inject_mouse_movements(
    regimes_file_path: str,
    *,
    duration_seconds: float | None = 5.0,
    stop_event: threading.Event | None = None,
    cdp_url: str = "http://127.0.0.1:9222/json",
) -> None:
    event = stop_event or threading.Event()
    if _deterministic_browser_io_active():
        log.info("[FakeMouse] deterministic browser I/O already active; refusing to start")
        return
    try:
        ws_url = _seed_websocket(cdp_url)
    except Exception as exc:
        log.warning("[FakeMouse] no safe seed became available: %s", exc)
        return
    fence_thread = threading.Thread(
        target=_watch_execution_fence, args=(event,),
        name="jobos-fake-mouse-execution-fence", daemon=True,
    )
    fence_thread.start()
    timer: threading.Timer | None = None
    if duration_seconds is not None:
        timer = threading.Timer(max(0.1, float(duration_seconds)), event.set)
        timer.daemon = True
        timer.start()
    try:
        _fake_mouse_routine(ws_url, regimes_file_path, event)
    finally:
        event.set()
        if timer is not None:
            timer.cancel()
        if fence_thread.is_alive():
            fence_thread.join(timeout=1.0)


def start_fake_mouse_thread(
    regimes_file_path: str,
    *,
    duration_seconds: float | None = 5.0,
    stop_event: threading.Event | None = None,
    cdp_url: str = "http://127.0.0.1:9222/json",
) -> threading.Thread:
    """Start FakeMouse and return the daemon thread.

    LinkedIn discovery passes its own ``stop_event`` and ``duration_seconds=None``
    so coverage lasts exactly for the browser-agent call.  Legacy callers retain
    the historical bounded five-second default.
    """
    thread = threading.Thread(
        target=inject_mouse_movements,
        args=(regimes_file_path,),
        kwargs={
            "duration_seconds": duration_seconds,
            "stop_event": stop_event,
            "cdp_url": cdp_url,
        },
        name="jobos-fake-mouse-compat",
        daemon=True,
    )
    thread.start()
    return thread
