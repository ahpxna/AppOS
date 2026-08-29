"""Public compatibility entrypoint for the retained FakeMouse feature.

The motion implementation lives in ``services.autofill.parallel_bypass``.  This
module is the public caller seam used by LinkedIn discovery so browser workers
do not import a private implementation function directly.
"""
from __future__ import annotations

import threading

import requests

from services.autofill.parallel_bypass import _fake_mouse_routine, _is_linkedin_url


def _seed_websocket(cdp_url: str = "http://127.0.0.1:9222/json") -> str:
    response = requests.get(cdp_url, timeout=3)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("CDP /json did not return a page list")
    tabs = [
        t for t in payload
        if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    linkedin = [t for t in tabs if _is_linkedin_url(str(t.get("url") or ""))]
    candidates = linkedin or tabs
    if len(candidates) != 1 and not linkedin:
        raise RuntimeError(f"legacy FakeMouse requires one exact non-LinkedIn page; found {len(candidates)}")
    if not candidates:
        raise RuntimeError("no eligible CDP page for FakeMouse")
    # Any LinkedIn seed is safe: the retained helper enumerates every live
    # LinkedIn page target and never enrolls non-LinkedIn tabs.
    return str(candidates[0]["webSocketDebuggerUrl"])


def inject_mouse_movements(
    regimes_file_path: str,
    *,
    duration_seconds: float | None = 5.0,
    stop_event: threading.Event | None = None,
    cdp_url: str = "http://127.0.0.1:9222/json",
) -> None:
    event = stop_event or threading.Event()
    ws_url = _seed_websocket(cdp_url)
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
