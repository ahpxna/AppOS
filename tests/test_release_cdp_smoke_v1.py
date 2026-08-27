from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

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
