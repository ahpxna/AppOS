#!/usr/bin/env python3
"""Controlled local ATS/CDP smoke gate for JobOS v0.1.0 releases.

This script is deliberately narrow:
- serves only the tracked local fake ATS fixture on loopback;
- creates the tracked fixture tab only through the already-configured loopback CDP endpoint;
- uses JobOS's managed OpenClaw transport for tab focus/snapshot/write/verification;
- pins the exact target id, snapshots it, fills one harmless
  text field, verifies the written value from a fresh snapshot, and closes it;
- never calls an agent/model, uploads a file, clicks submit, handles auth, or
  interacts with CAPTCHA/checkpoints.
"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ipaddress
from threading import Thread
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = REPO_ROOT / "tests" / "browser_fixtures"
FIXTURE_FILE = FIXTURE_DIR / "basic_form.html"
SMOKE_VALUE = "JobOS CDP Smoke"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


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


def _open_fixture_via_loopback_cdp(fixture_url: str) -> str:
    """Create only the controlled fixture tab without weakening OpenClaw SSRF policy.

    OpenClaw correctly blocks browser navigation to loopback/private destinations in
    strict mode.  The release fixture itself is operator-controlled and served on
    loopback, so seed that single tab through Chrome's loopback DevTools endpoint,
    then exercise OpenClaw for the actual focus/snapshot/fill/verify/close path.
    """
    endpoint = _assert_loopback_cdp() + "/json/new?" + quote(fixture_url, safe="")
    try:
        request = urllib.request.Request(
            endpoint, headers={"Accept": "application/json"}, method="PUT"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not create the controlled fixture tab through loopback CDP.") from exc
    target_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
    if not target_id:
        raise RuntimeError("Chrome CDP /json/new did not return a target id.")
    return target_id


def _field(snapshot: dict, label: str) -> dict:
    from services.autofill.autofill_agent_v1 import parse_snapshot
    wanted = " ".join(label.casefold().split())
    for node in parse_snapshot(snapshot):
        observed = " ".join(str(node.get("label") or "").casefold().split())
        if node.get("ref") and observed == wanted:
            return node
    raise RuntimeError(f"Local ATS fixture snapshot is missing field: {label}")


def _serve_fixture() -> tuple[ThreadingHTTPServer, Thread, str]:
    if not FIXTURE_FILE.is_file():
        raise RuntimeError(f"Tracked browser fixture is missing: {FIXTURE_FILE}")
    handler = partial(_QuietHandler, directory=str(FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, name="jobos-release-fixture", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}/basic_form.html?job=123"


def run_smoke() -> None:
    _assert_cdp_ready()
    from services.autofill.autofill_executor_v1 import OpenClawTransport

    server, thread, fixture_url = _serve_fixture()
    transport = OpenClawTransport(profile=os.getenv("JOBOS_BROWSER_PROFILE", "remote"), timeout=45)
    target_id: str | None = None
    try:
        target_id = _open_fixture_via_loopback_cdp(fixture_url)
        target = transport.focus(target_id)
        if target.url != fixture_url or transport.current_url(target_id) != fixture_url:
            raise RuntimeError("Pinned release-smoke tab did not stay on the exact local ATS fixture URL.")

        before = transport.snapshot(target_id)
        field = _field(before, "First name")
        ref = str(field["ref"])
        transport.execute(target_id, {"action": "fill", "target": ref, "value": SMOKE_VALUE})

        after = transport.snapshot(target_id)
        observed = str(_field(after, "First name").get("value") or "")
        if " ".join(observed.split()) != SMOKE_VALUE:
            raise RuntimeError(
                f"CDP fixture write verification failed: observed {observed!r}, expected {SMOKE_VALUE!r}."
            )
        if transport.current_url(target_id) != fixture_url:
            raise RuntimeError("Release-smoke tab navigated away from the controlled local ATS fixture.")
    finally:
        if target_id:
            try:
                transport.close(target_id)
            except Exception:
                pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
