from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from services.common.search_preferences import preference_reason, validate_location_pattern
from services.discovery.linkedin_discovery_v1 import normalize_jobs


ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_structured_detail_url_reduces_to_board_scope_not_detail_url():
    mod = _load("services/discovery/ats_source_enrollment_v1.py", "ats_source_scope_test")
    source = mod.derive_ats_source(
        "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/New-York/Engineer_123"
    )
    assert source is not None
    assert source.source_url == "https://acme.wd5.myworkdayjobs.com/en-US/Careers/"
    assert "/job/" not in source.source_url


def test_auto_enrollment_requires_exact_href_witness():
    mod = _load("services/discovery/ats_source_enrollment_v1.py", "ats_source_witness_test")

    class Cursor:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("DB must not be touched without snapshot href evidence")

    assert mod.enroll_ats_source(
        Cursor(), company="Acme",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/123",
        href_evidence="",
    ) is None


def test_malformed_row_does_not_abort_valid_linkedin_batch():
    rows = normalize_jobs({"jobs": [
        {"company": "Bad", "title": "Bad", "url": "https://linkedin.com/feed/", "jd_text": "x" * 300},
        {"company": "Good", "title": "Engineer", "url": "https://www.linkedin.com/jobs/view/123456/", "jd_text": "x" * 300},
    ]}, 2)
    assert [row["company"] for row in rows] == ["Good"]


def test_salary_unknown_stays_eligible_but_known_low_range_is_rejected():
    prefs = {"salary_floor": 100_000}
    assert preference_reason(company="A", title="Engineer", location="NY", work_mode="", preferences=prefs) is None
    assert preference_reason(company="A", title="Engineer", location="NY", work_mode="",
                             salary_range="$70k-$90k", preferences=prefs) == "published_salary_below_floor"


def test_location_regex_validation_and_runtime_fail_safe():
    with pytest.raises(ValueError):
        validate_location_pattern("[bad")
    with pytest.raises(ValueError):
        validate_location_pattern("(a+)+$")
    # Legacy malformed rows must not crash a discovery cycle.
    assert preference_reason(company="A", title="Engineer", location="Boston", work_mode="",
                             preferences={"location_allow_patterns": ["[bad"]}) == "location_not_allowed"


def test_autonomous_scheduler_is_explicitly_opt_in_and_capability_is_separate():
    source = (ROOT / "services" / "discovery" / "autonomous_discovery_v1.py").read_text()
    env = (ROOT / ".env.example").read_text()
    assert "JOBOS_AUTONOMOUS_DISCOVERY_ENABLED" in source
    assert "JOBOS_AUTONOMOUS_DISCOVERY_ENABLED=false" in env


def test_browser_lease_covers_declared_io_timeout_plus_grace():
    worker = (ROOT / "services" / "browser-controller" / "browser_queue_worker.py").read_text()
    assert "GREATEST(%s, timeout_seconds + %s)" in worker
    assert "LEASE_GRACE_SECONDS" in worker


def test_authwall_and_captcha_are_separate_paths_and_retry_restarts_mouse():
    worker = (ROOT / "services" / "browser-controller" / "browser_queue_worker.py").read_text()
    start = worker.index("def handle_discover_linkedin_jobs")
    end = worker.index("def handle_discover_linkedin_saved_jobs", start)
    handler = worker[start:end]
    assert "LinkedIn session requires manual re-authentication" in handler
    assert "_run_linkedin_agent_with_fake_mouse" in handler
    assert "after-captcha" in handler
