from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import base64
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("jobos_release_cdp_smoke", ROOT / "scripts" / "release_cdp_smoke.py")
assert SPEC and SPEC.loader
smoke = module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_release_smoke_selects_exact_field_from_openclaw_snapshot():
    snapshot = {
        "snapshot": '- textbox "First name" [ref=first]: "Ada"\n- textbox "Email" [ref=email]: ""',
        "refs": {
            "first": {"role": "textbox", "name": "First name"},
            "email": {"role": "textbox", "name": "Email"},
        },
    }
    assert smoke._field(snapshot, "First name")["ref"] == "first"
    assert smoke._field(snapshot, "First name")["value"] == "Ada"


def test_release_smoke_fixture_is_tracked_fake_ats_and_public_safe_url():
    assert smoke.FIXTURE_FILE == ROOT / "tests" / "browser_fixtures" / "basic_form.html"
    assert smoke.FIXTURE_FILE.is_file()
    assert smoke.FIXTURE_URL.startswith("https://example.com/")


def test_release_smoke_direct_script_bootstraps_repo_root_for_services_import():
    code = f"""
import runpy, sys
from pathlib import Path
root = Path({str(Path.cwd())!r})
script = root / 'scripts' / 'release_cdp_smoke.py'
sys.path[:] = [str(root / 'scripts')] + [p for p in sys.path if p not in ('', str(root))]
runpy.run_path(str(script), run_name='jobos_release_cdp_smoke_import_test')
assert str(root) in sys.path
import services
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_release_smoke_refuses_non_loopback_cdp(monkeypatch):
    monkeypatch.setenv("JOBOS_BROWSER_CDP_URL", "http://10.0.0.42:9222")
    try:
        smoke._assert_loopback_cdp()
    except RuntimeError as exc:
        assert "non-loopback" in str(exc)
    else:
        raise AssertionError("release smoke must refuse non-loopback CDP endpoints")


def test_release_smoke_creates_blank_target_through_loopback_cdp(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"id":"fixture-target","webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/page/fixture-target"}'

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), timeout))
        return Response()

    monkeypatch.setenv("JOBOS_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    target_id, ws_url = smoke._new_blank_target()
    assert target_id == "fixture-target"
    assert ws_url.endswith("/fixture-target")
    assert calls == [(
        "http://127.0.0.1:9222/json/new?about%3Ablank",
        "PUT",
        5,
    )]


def test_release_smoke_fulfills_tracked_fixture_via_cdp_without_private_page_url(monkeypatch):
    sent = []
    fixture_body = smoke.FIXTURE_FILE.read_bytes()

    class FakeWebSocket:
        def __init__(self):
            self.responses = []
            self.closed = False

        def send(self, raw):
            message = json.loads(raw)
            sent.append(message)
            call_id, method = message["id"], message["method"]
            if method == "Page.navigate":
                # Regression: Chrome may emit requestPaused before acknowledging
                # Page.navigate. The smoke must not discard this event.
                self.responses.append(json.dumps({
                    "method": "Fetch.requestPaused",
                    "params": {
                        "requestId": "request-1",
                        "request": {"url": smoke.FIXTURE_URL},
                    },
                }))
                self.responses.append(json.dumps({"id": call_id, "result": {"frameId": "frame"}}))
            elif method == "Runtime.evaluate":
                self.responses.append(json.dumps({
                    "id": call_id,
                    "result": {"result": {"value": f"complete|{smoke.FIXTURE_URL}|JobOS Fake ATS — Basic"}},
                }))
            else:
                self.responses.append(json.dumps({"id": call_id, "result": {}}))

        def recv(self):
            assert self.responses, "test fake websocket ran out of CDP responses"
            return self.responses.pop(0)

        def close(self):
            self.closed = True

    fake = FakeWebSocket()

    class FakeWebsocketModule:
        @staticmethod
        def create_connection(url, timeout, suppress_origin):
            assert url == "ws://127.0.0.1:9222/devtools/page/fixture-target"
            assert timeout == 8
            assert suppress_origin is True
            return fake

    monkeypatch.setitem(sys.modules, "websocket", FakeWebsocketModule)
    ws = smoke._prime_controlled_fixture(
        "ws://127.0.0.1:9222/devtools/page/fixture-target"
    )
    assert ws is fake
    navigate = next(item for item in sent if item["method"] == "Page.navigate")
    assert navigate["params"]["url"] == smoke.FIXTURE_URL
    fulfill = next(item for item in sent if item["method"] == "Fetch.fulfillRequest")
    assert base64.b64decode(fulfill["params"]["body"]) == fixture_body
    assert next(item for item in sent if item["method"] == "Network.setBlockedURLs")["params"] == {"urls": ["*"]}


def test_release_smoke_verifies_dom_when_efficient_snapshot_omits_live_value(monkeypatch):
    class Transport:
        def snapshot(self, target_id):
            assert target_id == "fixture-target"
            return {
                "snapshot": '- textbox "First name" [ref=first]: ""',
                "refs": {"first": {"role": "textbox", "name": "First name"}},
            }

    sent = []

    class FakeWebSocket:
        def __init__(self):
            self.responses = []

        def send(self, raw):
            message = json.loads(raw)
            sent.append(message)
            self.responses.append(json.dumps({
                "id": message["id"],
                "result": {"result": {"type": "string", "value": smoke.SMOKE_VALUE}},
            }))

        def recv(self):
            return self.responses.pop(0)

    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)
    smoke._verify_controlled_fill(Transport(), "fixture-target", FakeWebSocket(), timeout_seconds=0)
    assert sent
    assert sent[0]["method"] == "Runtime.evaluate"
    assert 'input[name="first_name"]' in sent[0]["params"]["expression"]


def test_release_smoke_fill_failure_reports_snapshot_and_dom_values(monkeypatch):
    class Transport:
        def snapshot(self, _target_id):
            return {
                "snapshot": '- textbox "First name" [ref=first]: ""',
                "refs": {"first": {"role": "textbox", "name": "First name"}},
            }

    class FakeWebSocket:
        def __init__(self):
            self.responses = []

        def send(self, raw):
            message = json.loads(raw)
            self.responses.append(json.dumps({
                "id": message["id"],
                "result": {"result": {"type": "string", "value": ""}},
            }))

        def recv(self):
            return self.responses.pop(0)

    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)
    try:
        smoke._verify_controlled_fill(Transport(), "fixture-target", FakeWebSocket(), timeout_seconds=0)
    except RuntimeError as exc:
        message = str(exc)
        assert "snapshot observed ''" in message
        assert "DOM observed ''" in message
        assert smoke.SMOKE_VALUE in message
    else:
        raise AssertionError("verification must fail when the actual DOM value is still empty")
