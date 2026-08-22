"""Bootstrap tests verify isolated roles without starting OpenClaw."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PATH = ROOT / "scripts" / "openclaw_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("jobos_openclaw_bootstrap_test", PATH)
bootstrap = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bootstrap)


def test_bootstrap_creates_role_overlays_and_disables_missing_optional_integrations(tmp_path):
    bootstrap.bootstrap(
        target_home=tmp_path,
        template_path=ROOT / "bootstrap" / "openclaw" / "openclaw.template.json",
        values={
            "GATEWAY_TOKEN": "unit-test-random-token",
            "BROWSER_CDP_URL": "http://browser:9222",
        },
        allow_missing=False,
        force=False,
    )

    config = bootstrap.load_json(tmp_path / ".openclaw" / "openclaw.json")
    assert config["browser"]["profiles"]["remote"]["cdpUrl"] == "http://browser:9222"
    assert config["hooks"]["enabled"] is False
    assert config["channels"]["telegram"]["enabled"] is False
    # Current OpenClaw owns search provider settings in its plugin config;
    # keeping the legacy tools.web.search key would make config validation fail.
    assert "google" not in config["plugins"]["entries"]
    assert "LinkedIn" in (tmp_path / ".openclaw" / "workspace-main" / "AGENTS.md").read_text()
    assert "approved profile assets" in (
        tmp_path / ".openclaw" / "workspace-resume" / "AGENTS.md"
    ).read_text()
    assert "worker reports" in (
        tmp_path / ".openclaw" / "workspace-repo_coordinator" / "AGENTS.md"
    ).read_text()
