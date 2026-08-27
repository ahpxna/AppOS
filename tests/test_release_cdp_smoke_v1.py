from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
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


def test_release_smoke_fixture_is_tracked_local_fake_ats():
    assert smoke.FIXTURE_FILE == ROOT / "tests" / "browser_fixtures" / "basic_form.html"
    assert smoke.FIXTURE_FILE.is_file()


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


def test_release_smoke_seeds_fixture_through_loopback_cdp_without_openclaw_navigation(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"id":"fixture-target"}'

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), timeout))
        return Response()

    monkeypatch.setenv("JOBOS_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    target_id = smoke._open_fixture_via_loopback_cdp(
        "http://127.0.0.1:43123/basic_form.html?job=123"
    )
    assert target_id == "fixture-target"
    assert calls == [(
        "http://127.0.0.1:9222/json/new?http%3A%2F%2F127.0.0.1%3A43123%2Fbasic_form.html%3Fjob%3D123",
        "PUT",
        5,
    )]
