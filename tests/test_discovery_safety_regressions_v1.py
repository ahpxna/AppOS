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


def test_agent_cannot_self_attest_external_apply_url_without_independent_dom_href():
    payload = {"jobs": [{
        "company": "Acme", "title": "Engineer",
        "url": "https://www.linkedin.com/jobs/view/123456/",
        "jd_text": "x" * 300,
        "apply_url": "https://jobs.lever.co/acme/abc",
        "apply_url_evidence": "https://jobs.lever.co/acme/abc",
    }]}
    unverified = normalize_jobs(payload, 1)
    assert unverified[0]["apply_url"] == ""
    assert unverified[0]["reported_apply_url"] == "https://jobs.lever.co/acme/abc"
    verified = normalize_jobs(payload, 1, verified_external_hrefs_by_job={"123456": ["https://jobs.lever.co/acme/abc"]})
    assert verified[0]["apply_url"] == "https://jobs.lever.co/acme/abc"
    assert verified[0]["apply_url_evidence"] == verified[0]["apply_url"]
    wrong_job = normalize_jobs(payload, 1, verified_external_hrefs_by_job={"999999": ["https://jobs.lever.co/acme/abc"]})
    assert wrong_job[0]["apply_url"] == ""


def test_autonomous_occurrence_keys_are_not_permanent_completed_task_keys():
    mod = _load("services/discovery/autonomous_discovery_v1.py", "autonomous_occurrence_key_test")
    base = "jobos:auto-linkedin:abc123"
    assert mod._occurrence_key(base, 100.0) != mod._occurrence_key(base, 101.0)
    assert mod._occurrence_key(base, 100.0).startswith(base + ":")


def test_structured_jsonld_exposes_explicit_salary_and_posted_date():
    mod = _load("services/ats/public_page.py", "public_page_salary_test")
    job = mod.normalize_jobposting({
        "@type": "JobPosting",
        "title": "Engineer",
        "description": "Responsibilities " + ("build systems " * 40),
        "url": "https://careers.example.com/jobs/1",
        "datePosted": "2026-08-20",
        "baseSalary": {"currency": "USD", "value": {"minValue": 120000, "maxValue": 160000}, "unitText": "YEAR"},
    }, page_url="https://careers.example.com/jobs/1", company_hint="Acme")
    assert job is not None
    assert job["posted_at"] == "2026-08-20"
    assert "120000-160000" in job["salary_range"]


def test_periodic_worker_escalates_repeated_child_failures_to_supervisor():
    source = (ROOT / "services" / "runtime" / "periodic_tasks_v1.py").read_text()
    assert "JOBOS_PERIODIC_FAILURE_EXIT_THRESHOLD" in source
    assert "return 1" in source


def test_telegram_saved_sync_passes_database_cursor():
    source = (ROOT / "services" / "telegram" / "telegram_review_bot_v1.py").read_text()
    start = source.index('if text in {"/saved_sync", "/saved"}')
    end = source.index('if text == "/discovery_status"', start)
    assert "queue_saved_sync_task(\n                    cur," in source[start:end]


def test_native_auto_enrollment_preserves_operator_disabled_state():
    mod = _load("services/discovery/ats_source_enrollment_v1.py", "ats_disabled_preserve_test")

    class Cursor:
        def __init__(self):
            self.row = None
            self.sql = []
        def execute(self, sql, params=()):
            self.sql.append(sql)
            low = " ".join(sql.lower().split())
            if "select id::text,company_name,enabled from ats_companies" in low:
                self.row = ("source-1", "Acme", False)
            elif "update ats_companies set notes=" in low and "returning id::text" in low:
                self.row = ("source-1",)
            else:
                self.row = None
        def fetchone(self):
            return self.row

    cur = Cursor()
    source_id = mod.enroll_ats_source(
        cur, company="Acme",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/123",
        href_evidence="https://job-boards.greenhouse.io/acme/jobs/123",
    )
    assert source_id == "source-1"
    assert not any("update ats_companies set enabled" in " ".join(sql.lower().split()) for sql in cur.sql)


def test_manual_structured_source_is_not_overwritten_by_auto_refresh():
    mod = _load("services/discovery/ats_source_enrollment_v1.py", "ats_manual_source_test")

    class Cursor:
        def __init__(self):
            self.row = None
            self.rows = []
            self.sql = []
        def execute(self, sql, params=()):
            self.sql.append((sql, params))
            low = " ".join(sql.lower().split())
            self.row, self.rows = None, []
            if "where ats_platform=%s and source_url=%s for update" in low:
                self.row = None
            elif "lower(trim(company_name))=lower(trim(%s))" in low:
                self.rows = [("manual-1", "https://old.example/jobs/", "operator configured")]
            elif low.startswith("insert into ats_companies"):
                self.row = ("auto-2",)
        def fetchone(self):
            return self.row
        def fetchall(self):
            return self.rows

    cur = Cursor()
    source_id = mod.enroll_ats_source(
        cur, company="Acme",
        apply_url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_123",
        href_evidence="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_123",
    )
    assert source_id == "auto-2"
    assert not any("set source_url=" in " ".join(sql.lower().split()) for sql, _ in cur.sql)


def test_ats_deadlines_are_clamped_inside_periodic_parent_boundary(monkeypatch):
    monkeypatch.setenv("JOBOS_ATS_PERIODIC_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("JOBOS_ATS_PROCESS_DEADLINE_SECONDS", "1200")
    monkeypatch.setenv("JOBOS_ATS_RUN_DEADLINE_SECONDS", "1200")
    mod = _load("services/discovery/ats_discovery_v1.py", "ats_deadline_clamp_test")
    assert mod.ATS_PROCESS_DEADLINE_SECONDS <= mod.ATS_PERIODIC_TIMEOUT_SECONDS - mod.ATS_FINALIZATION_GRACE_SECONDS
    assert mod.ATS_RUN_DEADLINE_SECONDS <= mod.ATS_PROCESS_DEADLINE_SECONDS


def test_ats_html_extractor_does_not_duplicate_visible_text():
    mod = _load("services/discovery/ats_discovery_v1.py", "ats_html_text_test")
    assert mod.html_to_text("<p>Hello world</p>") == "Hello world"


def test_exact_location_alternation_can_drive_search_without_regex_syntax(monkeypatch):
    mod = _load("services/discovery/autonomous_discovery_v1.py", "location_alternation_test")
    monkeypatch.setattr(mod, "approved_terms", lambda cur: ["Engineer"])
    monkeypatch.setattr(mod, "_preferences", lambda cur: {
        "location_allow_patterns": [r"^(Boston|New York)$"],
        "allowed_work_modes": [], "allowed_employment_types": [], "freshness_days": 7,
    })
    monkeypatch.setenv("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", "2")
    plan = mod.build_linkedin_plan(object(), now=1.0)
    assert {item["location"].casefold() for item in plan} == {"boston", "new york"}
