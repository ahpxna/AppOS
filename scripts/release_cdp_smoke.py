#!/usr/bin/env python3
"""Controlled ATS/CDP smoke gate for JobOS v0.1.0 releases.

This script is deliberately narrow:
- reads only the tracked fake ATS fixture from the repository;
- creates one exact tab through the already-configured loopback CDP endpoint;
- fulfills a reserved public-safe HTTPS URL with the tracked fixture bytes through
  CDP request interception, so OpenClaw never needs private-network browser access;
- uses JobOS's managed OpenClaw transport for focus/snapshot/write/verification;
- never calls an agent/model, uploads a file, clicks submit, handles auth, or
  interacts with CAPTCHA/checkpoints.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_FILE = REPO_ROOT / "tests" / "browser_fixtures" / "basic_form.html"
FIXTURE_URL = "https://example.com/__jobos_release_fixture__?job=123"
SMOKE_VALUE = "JobOS CDP Smoke"


def _cdp_base_url() -> str:
    return (
        os.getenv("JOBOS_BROWSER_CDP_URL")
        or os.getenv("OPENCLAW_BROWSER_CDP_URL")
        or "http://127.0.0.1:9222"
    ).rstrip("/")


def _assert_loopback_cdp() -> str:
    base_url = _cdp_base_url()
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "http" or not host:
        raise RuntimeError("Release CDP smoke requires an HTTP loopback CDP endpoint.")
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise RuntimeError("Release CDP smoke refuses a non-loopback CDP endpoint.")
        except ValueError as exc:
            raise RuntimeError("Release CDP smoke refuses a non-loopback CDP endpoint.") from exc
    return base_url


def _assert_cdp_ready() -> None:
    url = _assert_loopback_cdp() + "/json/version"
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Chrome CDP is not reachable at {_cdp_base_url()}. "
            "Start the isolated JobOS browser before release verification."
        ) from exc
    if not isinstance(payload, dict) or not payload.get("Browser") or not payload.get("webSocketDebuggerUrl"):
        raise RuntimeError("Chrome CDP /json/version returned an incomplete payload.")


def _new_blank_target() -> tuple[str, str]:
    endpoint = _assert_loopback_cdp() + "/json/new?" + quote("about:blank", safe="")
    try:
        request = urllib.request.Request(
            endpoint, headers={"Accept": "application/json"}, method="PUT"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not create the controlled release-smoke tab through loopback CDP.") from exc
    target_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
    websocket_url = str(payload.get("webSocketDebuggerUrl") or "") if isinstance(payload, dict) else ""
    if not target_id or not websocket_url.startswith("ws"):
        raise RuntimeError("Chrome CDP /json/new did not return an exact target id and WebSocket URL.")
    return target_id, websocket_url


def _cdp_send(ws, call_id: int, method: str, params: dict | None = None) -> None:
    ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}, separators=(",", ":")))


def _cdp_recv_json(ws, stage: str) -> dict:
    try:
        raw = ws.recv()
    except Exception as exc:
        raise RuntimeError(f"Chrome CDP timed out while {stage}: {exc}") from exc
    try:
        message = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Chrome CDP returned invalid JSON while {stage}.") from exc
    if not isinstance(message, dict):
        raise RuntimeError(f"Chrome CDP returned a non-object message while {stage}.")
    return message


def _cdp_wait_for_id(ws, call_id: int, *, stage: str | None = None) -> dict:
    stage = stage or f"waiting for command {call_id}"
    while True:
        message = _cdp_recv_json(ws, stage)
        if message.get("id") != call_id:
            continue
        if message.get("error"):
            raise RuntimeError(f"Chrome CDP {call_id} failed: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _prime_controlled_fixture(websocket_url: str):
    """Fulfill the tracked fixture at a public-safe URL without weakening SSRF policy."""
    if not FIXTURE_FILE.is_file():
        raise RuntimeError(f"Tracked browser fixture is missing: {FIXTURE_FILE}")
    fixture_bytes = FIXTURE_FILE.read_bytes()
    try:
        import websocket
        ws = websocket.create_connection(websocket_url, timeout=8, suppress_origin=True)
    except Exception as exc:
        raise RuntimeError("Could not attach to the exact release-smoke target through Chrome CDP.") from exc

    try:
        _cdp_send(ws, 1, "Page.enable")
        _cdp_wait_for_id(ws, 1)
        _cdp_send(ws, 2, "Fetch.enable", {
            "patterns": [{"urlPattern": FIXTURE_URL, "requestStage": "Request"}],
        })
        _cdp_wait_for_id(ws, 2)
        _cdp_send(ws, 3, "Page.navigate", {"url": FIXTURE_URL})

        # Page.navigate's response and Fetch.requestPaused are asynchronous.
        # Chrome may deliver requestPaused first. Waiting only for command id=3
        # would discard that event and then block forever waiting for an event
        # that already arrived. Fulfill the exact controlled request immediately
        # whenever it arrives, and independently wait for both acknowledgements.
        navigate_acked = False
        fulfill_sent = False
        fulfill_acked = False
        while not (navigate_acked and fulfill_acked):
            message = _cdp_recv_json(ws, "priming the controlled fixture")

            if message.get("id") == 3:
                if message.get("error"):
                    raise RuntimeError(f"Chrome CDP Page.navigate failed: {message['error']}")
                navigate_acked = True
                continue

            if message.get("id") == 4:
                if message.get("error"):
                    raise RuntimeError(f"Chrome CDP Fetch.fulfillRequest failed: {message['error']}")
                fulfill_acked = True
                continue

            if message.get("method") != "Fetch.requestPaused":
                continue
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            request = params.get("request") if isinstance(params.get("request"), dict) else {}
            request_url = str(request.get("url") or "")
            if request_url != FIXTURE_URL:
                raise RuntimeError(
                    f"Chrome CDP paused an unexpected release-smoke URL: {request_url!r}."
                )
            request_id = str(params.get("requestId") or "")
            if not request_id:
                raise RuntimeError("Chrome CDP paused the controlled fixture without a request id.")
            if fulfill_sent:
                continue
            _cdp_send(ws, 4, "Fetch.fulfillRequest", {
                "requestId": request_id,
                "responseCode": 200,
                "responsePhrase": "OK",
                "responseHeaders": [
                    {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                    {"name": "Cache-Control", "value": "no-store"},
                ],
                "body": base64.b64encode(fixture_bytes).decode("ascii"),
            })
            fulfill_sent = True

        # Keep this CDP session alive until smoke completion and block any later
        # network request from the controlled document. The fixture itself has no
        # external resources, but this closes accidental future additions.
        _cdp_send(ws, 5, "Fetch.disable")
        _cdp_wait_for_id(ws, 5)
        _cdp_send(ws, 6, "Network.enable")
        _cdp_wait_for_id(ws, 6)
        _cdp_send(ws, 7, "Network.setBlockedURLs", {"urls": ["*"]})
        _cdp_wait_for_id(ws, 7)

        _cdp_send(ws, 8, "Runtime.evaluate", {
            "expression": "document.readyState + '|' + location.href + '|' + document.title",
            "returnByValue": True,
        })
        result = _cdp_wait_for_id(ws, 8)
        value = (((result.get("result") or {}) if isinstance(result, dict) else {}).get("value") or "")
        if FIXTURE_URL not in str(value) or "JobOS Fake ATS" not in str(value):
            raise RuntimeError("Controlled fixture did not materialize on the exact release-smoke target.")
        return ws
    except Exception:
        try:
            ws.close()
        except Exception:
            pass
        raise



def _cdp_controlled_input_value(ws, call_id: int) -> str:
    """Read the exact controlled textbox value from the pinned target via CDP."""
    _cdp_send(ws, call_id, "Runtime.evaluate", {
        "expression": "(() => { const el = document.querySelector('input[name=\"first_name\"]'); return el ? String(el.value ?? '') : null; })()",
        "returnByValue": True,
    })
    result = _cdp_wait_for_id(ws, call_id, stage="verifying the controlled textbox value")
    remote = result.get("result") if isinstance(result, dict) else None
    if not isinstance(remote, dict) or remote.get("value") is None:
        raise RuntimeError("Controlled ATS fixture no longer exposes the exact First name input.")
    return str(remote.get("value") or "")


def _verify_controlled_fill(transport, target_id: str, cdp_ws, *, timeout_seconds: float = 5.0) -> None:
    """Bounded verification of an OpenClaw fill on the exact controlled target.

    OpenClaw efficient role snapshots are authoritative for ref discovery/rematching,
    but they are not a reliable serialization of live textbox values on every driver.
    Therefore the gate re-snapshots/rematches after the write and confirms the final
    DOM value read-only through the already-pinned loopback CDP session.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    attempt = 0
    last_snapshot_value = ""
    last_dom_value = ""
    while True:
        attempt += 1
        after = transport.snapshot(target_id)
        last_snapshot_value = str(_field(after, "First name").get("value") or "")
        last_dom_value = _cdp_controlled_input_value(cdp_ws, 20 + attempt)
        if " ".join(last_dom_value.split()) == SMOKE_VALUE:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "CDP fixture write verification failed: "
                f"snapshot observed {last_snapshot_value!r}, DOM observed {last_dom_value!r}, "
                f"expected {SMOKE_VALUE!r}."
            )
        time.sleep(0.1)

def _field(snapshot: dict, label: str) -> dict:
    from services.autofill.autofill_agent_v1 import parse_snapshot
    wanted = " ".join(label.casefold().split())
    for node in parse_snapshot(snapshot):
        observed = " ".join(str(node.get("label") or "").casefold().split())
        if node.get("ref") and observed == wanted:
            return node
    raise RuntimeError(f"Controlled ATS fixture snapshot is missing field: {label}")


def run_smoke() -> None:
    _assert_cdp_ready()
    from services.autofill.autofill_executor_v1 import OpenClawTransport

    transport = OpenClawTransport(profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"), timeout=45)
    target_id: str | None = None
    cdp_ws = None
    try:
        target_id, websocket_url = _new_blank_target()
        cdp_ws = _prime_controlled_fixture(websocket_url)
        target = transport.focus(target_id)
        if target.url != FIXTURE_URL or transport.current_url(target_id) != FIXTURE_URL:
            raise RuntimeError("Pinned release-smoke tab did not stay on the exact controlled fixture URL.")

        before = transport.snapshot(target_id)
        field = _field(before, "First name")
        ref = str(field["ref"])
        transport.execute(target_id, {"action": "fill", "target": ref, "value": SMOKE_VALUE})

        _verify_controlled_fill(transport, target_id, cdp_ws)
        if transport.current_url(target_id) != FIXTURE_URL:
            raise RuntimeError("Release-smoke tab navigated away from the controlled ATS fixture.")
    finally:
        if target_id:
            try:
                transport.close(target_id)
            except Exception:
                pass
        if cdp_ws is not None:
            try:
                cdp_ws.close()
            except Exception:
                pass


def main() -> int:
    try:
        run_smoke()
    except Exception as exc:
        print(f"CONTROLLED CDP FIXTURE: FAIL: {exc}", file=sys.stderr)
        return 1
    print("CONTROLLED CDP FIXTURE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
