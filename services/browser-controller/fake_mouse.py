"""Compatibility entrypoint for the retained FakeMouse feature.

The active implementation lives in services.autofill.parallel_bypass, where CDP
I/O is timeout-bounded and LinkedIn discovery fans out only to LinkedIn page
targets.  Keep this legacy module as a safe adapter so an old import cannot
silently revive the historical first-tab/no-timeout implementation.
"""
from __future__ import annotations

import threading
import time

import requests

from services.autofill.parallel_bypass import _fake_mouse_routine, _is_linkedin_url


def _seed_websocket(cdp_url: str = "http://127.0.0.1:9222/json") -> str:
    response = requests.get(cdp_url, timeout=3)
    response.raise_for_status()
    tabs = [t for t in response.json() if isinstance(t, dict) and t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")]
    linkedin = [t for t in tabs if _is_linkedin_url(str(t.get("url") or ""))]
    candidates = linkedin or tabs
    if len(candidates) != 1 and not linkedin:
        raise RuntimeError(f"legacy FakeMouse requires one exact non-LinkedIn page; found {len(candidates)}")
    if not candidates:
        raise RuntimeError("no eligible CDP page for FakeMouse")
    # Any LinkedIn seed is safe: the retained helper enumerates all LinkedIn tabs.
    return str(candidates[0]["webSocketDebuggerUrl"])


def inject_mouse_movements(regimes_file_path: str, *, duration_seconds: float = 5.0) -> None:
    stop_event = threading.Event()
    ws_url = _seed_websocket()
    timer = threading.Timer(max(0.1, float(duration_seconds)), stop_event.set)
    timer.daemon = True
    timer.start()
    try:
        _fake_mouse_routine(ws_url, regimes_file_path, stop_event)
    finally:
        stop_event.set()
        timer.cancel()


def start_fake_mouse_thread(regimes_file_path: str, *, duration_seconds: float = 5.0) -> threading.Thread:
    thread = threading.Thread(
        target=inject_mouse_movements,
        args=(regimes_file_path,),
        kwargs={"duration_seconds": duration_seconds},
        name="jobos-fake-mouse-compat",
        daemon=True,
    )
    thread.start()
    return thread
