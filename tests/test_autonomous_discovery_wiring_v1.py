from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _REGEXY(value: str) -> bool:
    return any(ch in value for ch in "*[]()|^$+?{}\\")


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_ats_source_enrollment_derives_only_grounded_deterministic_locators():
    mod = _load("services/discovery/ats_source_enrollment_v1.py", "ats_source_enrollment_test")
    greenhouse = mod.derive_ats_source("https://job-boards.greenhouse.io/acme/jobs/123")
    assert greenhouse.platform == "greenhouse"
    assert greenhouse.slug == "acme"
    assert greenhouse.source_url is None

    lever = mod.derive_ats_source("https://jobs.lever.co/acme/abc-123")
    assert lever.platform == "lever"
    assert lever.slug == "acme"

    workday = mod.derive_ats_source("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123")
    assert workday.platform == "workday"
    assert workday.slug is None
    assert workday.source_url.startswith("https://acme.wd5.myworkdayjobs.com/")

    assert mod.derive_ats_source("https://example.com/jobs/123") is None
    # No company slug may be invented from a direct Workable /j/ URL.
    assert mod.derive_ats_source("https://apply.workable.com/j/ABC123/") is None


def test_profile_planner_builds_bounded_searches_from_terms_and_preferences(monkeypatch):
    mod = _load("services/discovery/autonomous_discovery_v1.py", "autonomous_discovery_plan_test")
    monkeypatch.setattr(mod, "approved_terms", lambda cur: ["Data Engineer", "Python", "AWS"])
    monkeypatch.setattr(mod, "_preferences", lambda cur: {
        "location_allow_patterns": ["New York, NY", r"Boston.*"],
        "allowed_work_modes": ["remote", "hybrid"],
        "allowed_employment_types": ["full-time"],
        "freshness_days": 7,
    })
    monkeypatch.setenv("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", "2")
    monkeypatch.setenv("JOBOS_LINKEDIN_DISCOVERY_MAX_RESULTS", "3")
    monkeypatch.setenv("JOBOS_LINKEDIN_DISCOVERY_COOLDOWN_SECONDS", "3600")
    plan = mod.build_linkedin_plan(object(), now=7200)
    assert len(plan) == 2
    assert all(item["max_results"] == 3 for item in plan)
    assert all(item["date_posted"] == "week" for item in plan)
    assert all(item["work_modes"] == ["remote", "hybrid"] for item in plan)
    assert all(item["employment_types"] == ["full-time"] for item in plan)
    # Regex patterns stay post-intake filters; they are not sent to LinkedIn as locations.
    assert all(item["location"] != r"Boston.*" for item in plan)
    assert all(not _REGEXY(item["location"]) for item in plan)


def test_runtime_and_cli_have_inbound_edges_for_profile_discovery():
    supervisor = (ROOT / "scripts/jobos_runtime_supervisor.py").read_text()
    periodic = (ROOT / "services/runtime/periodic_tasks_v1.py").read_text()
    jobos = (ROOT / "scripts/jobos.py").read_text()
    planner = (ROOT / "services/discovery/autonomous_discovery_v1.py").read_text()
    assert '"profile-discovery": WorkerSpec(' in supervisor
    assert '"profile-discovery": ([sys.executable, "-m", "services.discovery.autonomous_discovery_v1", "run", "--apply"]' in periodic
    assert 'commands.add_parser("discover"' in jobos
    assert 'discover_sub.add_parser("linkedin"' in jobos
    assert "from services.discovery.profile_job_search_v1 import approved_terms" in planner
    assert "queue_discovery_task" in planner
    assert "queue_saved_sync_task" in planner


def test_browser_claim_carries_requested_by_and_autonomous_tasks_remain_bounded():
    worker = (ROOT / "services/browser-controller/browser_queue_worker.py").read_text()
    assert "autofill_action_scope, requested_by;" in worker
    assert '"requested_by": row[18]' in worker
    assert 'task.get("requested_by") == "profile_autonomous_discovery_v1"' in worker
    assert "apply_url" in worker
    assert "exact external employer/ATS apply URL if visibly grounded, otherwise empty" in worker


def test_linkedin_intake_has_bucket_idempotency_and_preference_aware_autonomous_payload():
    intake = (ROOT / "services/discovery/linkedin_intake_v1.py").read_text()
    discovery = (ROOT / "services/discovery/linkedin_discovery_v1.py").read_text()
    ats = (ROOT / "services/discovery/ats_discovery_v1.py").read_text()
    planner = (ROOT / "services/discovery/autonomous_discovery_v1.py").read_text()
    assert "pg_advisory_xact_lock(hashtext(%s))" in intake
    assert '"apply_search_preferences": bool(autonomous)' in intake
    assert "idempotency_key" in intake
    assert "preference_reason(" in discovery
    assert "max_active_applications_per_employer" in discovery
    assert "enroll_ats_source(" in discovery
    assert "preference_reason(" in ats
    assert "existing postings still flow" in ats.casefold()
    assert "_enroll_observed_ats_sources" in planner


def test_saved_jobs_periodic_path_is_planner_wired_not_manual_only():
    planner = (ROOT / "services/discovery/autonomous_discovery_v1.py").read_text()
    assert 'JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED' in planner
    assert 'jobos:auto-linkedin-saved:' in planner
    assert 'queue_saved_sync_task(' in planner
