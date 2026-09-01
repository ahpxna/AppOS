from __future__ import annotations

import importlib.util
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "services" / "browser-controller" / "fake_mouse.py"
    spec = importlib.util.spec_from_file_location("jobos_fake_mouse_fence_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_waits_for_linkedin_and_does_not_require_unique_linkedin_tab(monkeypatch):
    module = _module()
    payloads = iter([
        [],
        [
            {"type": "page", "url": "https://www.linkedin.com/jobs/search/", "webSocketDebuggerUrl": "ws://search"},
            {"type": "page", "url": "https://www.linkedin.com/jobs/view/1", "webSocketDebuggerUrl": "ws://detail"},
            {"type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://other"},
        ],
    ])

    class Response:
        def raise_for_status(self): pass
        def json(self): return next(payloads)

    monkeypatch.setattr(module.requests, "get", lambda *_a, **_k: Response())
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    assert module._seed_websocket(wait_seconds=1) == "ws://search"


def test_fake_mouse_refuses_to_start_during_deterministic_io(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_deterministic_browser_io_active", lambda: True)
    monkeypatch.setattr(module, "_seed_websocket", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    called = []
    monkeypatch.setattr(module, "_fake_mouse_routine", lambda *_a: called.append(True))
    module.inject_mouse_movements("regimes.json")
    assert called == []


def test_fence_monitor_stops_live_multitab_controller(monkeypatch):
    module = _module()
    states = iter([False, True])
    monkeypatch.setattr(module, "_deterministic_browser_io_active", lambda: next(states))
    event = threading.Event()
    monkeypatch.setattr(event, "wait", lambda _seconds: False)
    module._watch_execution_fence(event)
    assert event.is_set()


def test_motion_implementation_has_no_click_focus_or_navigation_commands():
    source = (ROOT / "services" / "autofill" / "parallel_bypass.py").read_text()
    routine = source[source.index("def _fake_mouse_target_routine"):source.index("def _fake_mouse_linkedin_controller")]
    assert '"type": "mouseMoved"' in routine
    for forbidden in ("mousePressed", "mouseReleased", "Page.navigate", "Target.activateTarget"):
        assert forbidden not in routine
