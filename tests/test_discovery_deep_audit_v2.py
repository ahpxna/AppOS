from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from services.ats.contracts import WorkMode, infer_work_mode
from services.common.config import env_float, env_int
from services.common.search_preferences import preference_reason

ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_work_mode_authority_normalizes_on_site_and_does_not_reject_unknown():
    defaults = {"allowed_work_modes": ["remote", "hybrid", "on-site"]}
    assert preference_reason(
        company="Acme", title="Engineer", location="NY", work_mode="on_site", preferences=defaults
    ) is None
    assert preference_reason(
        company="Acme", title="Engineer", location="NY", work_mode="unknown", preferences=defaults
    ) is None
    assert preference_reason(
        company="Acme", title="Engineer", location="NY", work_mode="unknown",
        preferences={"allowed_work_modes": ["remote"]},
    ) == "work_mode_not_preferred"
    assert preference_reason(
        company="Acme", title="Engineer", location="NY", work_mode="on-site",
        preferences={"allowed_work_modes": ["remote"]},
    ) == "work_mode_not_preferred"
    assert infer_work_mode("", "NY", "This position is on-site in our office.") == WorkMode.ON_SITE


def test_location_regex_cli_preserves_commas_and_supports_json_array():
    mod = _load("services/discovery/search_preferences_v1.py", "search_preferences_cli_comma_test")
    assert mod.regex_list(r"^New York, NY$") == [r"^New York, NY$"]
    assert mod.regex_list("Boston,Seattle") == ["Boston", "Seattle"]
    assert mod.regex_list(json.dumps([r"^New York, NY$", r"^Boston, MA$"])) == [
        r"^New York, NY$", r"^Boston, MA$"
    ]
    with pytest.raises(ValueError):
        mod.regex_list('[null]')


def test_db_migration_makes_regex_validation_a_database_authority():
    sql = (ROOT / "db/migrations/099_discovery_preference_authority.sql").read_text()
    assert "CREATE OR REPLACE FUNCTION jobos_validate_job_search_preferences" in sql
    assert "BEFORE INSERT OR UPDATE OF location_allow_patterns" in sql
    assert "SQLSTATE '2201B'" in sql
    assert "pattern IS NULL" in sql
    assert "UPDATE job_search_preferences" in sql


def test_ats_env_typos_fall_back_instead_of_crashing_import(monkeypatch):
    monkeypatch.setenv("JOBOS_ATS_PERIODIC_TIMEOUT_SECONDS", "abc")
    monkeypatch.setenv("JOBOS_ATS_PROCESS_DEADLINE_SECONDS", "abc")
    monkeypatch.setenv("JOBOS_ATS_RUN_DEADLINE_SECONDS", "abc")
    monkeypatch.setenv("JOBOS_ATS_DETAIL_REQUEST_BUDGET", "abc")
    mod = _load("services/discovery/ats_discovery_v1.py", "ats_invalid_env_test")
    assert mod.ATS_PERIODIC_TIMEOUT_SECONDS == 1320
    assert mod.ATS_PROCESS_DEADLINE_SECONDS == 1200
    assert mod.ATS_RUN_DEADLINE_SECONDS == 1200
    assert mod.DETAIL_REQUEST_BUDGET >= 1


def test_common_numeric_env_parser_is_bounded_and_typo_safe(monkeypatch):
    monkeypatch.setenv("JOBOS_TEST_BAD_INT", "not-an-int")
    monkeypatch.setenv("JOBOS_TEST_BAD_FLOAT", "not-a-float")
    assert env_int("JOBOS_TEST_BAD_INT", 7, minimum=2, maximum=9) == 7
    assert env_float("JOBOS_TEST_BAD_FLOAT", 3.5, minimum=1, maximum=4) == 3.5
    monkeypatch.setenv("JOBOS_TEST_BAD_INT", "999")
    assert env_int("JOBOS_TEST_BAD_INT", 7, minimum=2, maximum=9) == 9


def test_existing_auto_enrollment_never_overwrites_operator_notes():
    mod = _load("services/discovery/ats_source_enrollment_v1.py", "ats_notes_preserved_test")

    class Cursor:
        def __init__(self):
            self.row = None
            self.sql: list[str] = []

        def execute(self, sql, params=()):
            low = " ".join(sql.lower().split())
            self.sql.append(low)
            if "select id::text,company_name,enabled from ats_companies" in low:
                self.row = ("source-1", "Acme", False)
            else:
                self.row = None

        def fetchone(self):
            return self.row

    cur = Cursor()
    assert mod.enroll_ats_source(
        cur,
        company="Acme",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/123",
        href_evidence="https://job-boards.greenhouse.io/acme/jobs/123",
        company_evidence="Acme",
    ) == "source-1"
    assert not any("update ats_companies" in sql for sql in cur.sql)


def test_linkedin_cleanup_closes_only_new_job_detail_tabs(monkeypatch):
    mod = _load("services/browser-controller/browser_queue_worker.py", "linkedin_tab_cleanup_test")
    closed: list[str] = []

    class Response:
        def __init__(self, payload=None, ok=True):
            self._payload = payload
            self.ok = ok

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    tabs = [
        {"id": "old", "type": "page", "url": "https://www.linkedin.com/jobs/view/111/"},
        {"id": "new", "type": "page", "url": "https://www.linkedin.com/jobs/view/222/"},
        {"id": "search", "type": "page", "url": "https://www.linkedin.com/jobs/search/?keywords=x"},
    ]

    def fake_get(url, timeout=3):
        if "/json/close/" in url:
            closed.append(url.rsplit("/", 1)[-1])
            return Response(ok=True)
        return Response(tabs)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    assert mod._close_linkedin_detail_tabs_opened_after({"old"}) == 1
    assert closed == ["new"]


def test_linkedin_cleanup_fails_safe_when_cdp_baseline_is_unknown(monkeypatch):
    mod = _load("services/browser-controller/browser_queue_worker.py", "linkedin_tab_cleanup_unknown_test")

    def fail_get(*args, **kwargs):
        raise RuntimeError("cdp temporarily unavailable")

    monkeypatch.setattr(mod.requests, "get", fail_get)
    assert mod._linkedin_detail_target_ids() is None
    # process_one only invokes cleanup when the captured baseline is not None.
    worker = (ROOT / "services/browser-controller/browser_queue_worker.py").read_text()
    assert "if linkedin_detail_targets_before is not None:" in worker


def test_process_one_wires_cleanup_for_both_linkedin_discovery_handlers():
    worker = (ROOT / "services/browser-controller/browser_queue_worker.py").read_text()
    start = worker.index("def process_one")
    body = worker[start:]
    assert '{"discover_linkedin_jobs", "discover_linkedin_saved_jobs"}' in body
    assert "_close_linkedin_detail_tabs_opened_after(linkedin_detail_targets_before)" in body


def test_saved_sync_reserves_first_available_queue_slot(monkeypatch):
    mod = _load("services/discovery/autonomous_discovery_v1.py", "saved_priority_test")
    calls: list[str] = []

    monkeypatch.setenv("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JOBOS_AUTONOMOUS_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JOBOS_DISCOVERY_MAX_QUEUED_TASKS", "1")
    monkeypatch.setattr(mod, "_enroll_observed_ats_sources", lambda cur: 0)
    monkeypatch.setattr(mod, "_active_planner_tasks", lambda cur: 0)
    monkeypatch.setattr(mod, "_ensure_scheduler_state", lambda cur, key: (0, None, None))
    monkeypatch.setattr(mod, "_failed_task_action", lambda *a, **k: (None, None))
    monkeypatch.setattr(mod, "_coverage_requirements", lambda cur: {
        "matrix_size": 10, "target_hours": 24, "cycles_in_target": 96,
        "required_searches_per_cycle": 3,
    })
    monkeypatch.setattr(mod, "approved_terms", lambda cur: ["Engineer"])

    def plan(cur, *, now=None):
        return [{"keywords": "Engineer", "location": "", "max_results": 3}]

    monkeypatch.setattr(mod, "build_linkedin_plan", plan)

    def queue_saved(cur, **kwargs):
        calls.append("saved")
        return "sync-1", "task-saved", True

    def queue_search(cur, **kwargs):
        calls.append("search")
        return "task-search", True

    monkeypatch.setattr(mod, "queue_saved_sync_task", queue_saved)
    monkeypatch.setattr(mod, "queue_discovery_task", queue_search)

    class Cursor:
        def execute(self, sql, params=()):
            self.last = " ".join(sql.lower().split())

        def fetchone(self):
            return (0,)

    result = mod.run_once(Cursor(), apply=True, now=1000)
    assert calls == ["saved"]
    assert result["saved_sync"]["created"] is True
    assert result["queued_searches"] == []


def test_queue_admission_scans_past_cooldown_candidates(monkeypatch):
    mod = _load("services/discovery/autonomous_discovery_v1.py", "coverage_scan_test")
    monkeypatch.setenv("JOBOS_LINKEDIN_AGENT_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JOBOS_AUTONOMOUS_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JOBOS_LINKEDIN_SAVED_DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("JOBOS_DISCOVERY_MAX_QUEUED_TASKS", "1")
    monkeypatch.setattr(mod, "_enroll_observed_ats_sources", lambda cur: 0)
    monkeypatch.setattr(mod, "_active_planner_tasks", lambda cur: 0)
    monkeypatch.setattr(mod, "approved_terms", lambda cur: ["A", "B"] )
    monkeypatch.setattr(mod, "_coverage_requirements", lambda cur: {
        "matrix_size": 2, "target_hours": 24, "cycles_in_target": 96,
        "required_searches_per_cycle": 1,
    })
    planned = [
        {"keywords": "cooldown", "location": "", "max_results": 3},
        {"keywords": "due", "location": "", "max_results": 3},
    ]
    monkeypatch.setattr(mod, "build_linkedin_plan", lambda cur, **kwargs: planned)
    monkeypatch.setattr(mod, "_failed_task_action", lambda *a, **k: (None, None))

    queued: list[str] = []
    def ensure_state(cur, key):
        if key == mod.PLANNER_KEY:
            return (0, None, None)
        if key.startswith(mod.SEARCH_STATE_PREFIX) and len(queued) == 0:
            # First candidate is still in cooldown; second is due.
            from datetime import datetime, timezone
            if not getattr(cur, "seen_search_state", False):
                cur.seen_search_state = True
                return (0, datetime.fromtimestamp(999, tz=timezone.utc), None)
        return (0, None, None)
    monkeypatch.setattr(mod, "_ensure_scheduler_state", ensure_state)
    monkeypatch.setattr(mod, "queue_discovery_task", lambda cur, request, **kwargs: (queued.append(request["keywords"]) or "task", True))

    class Cursor:
        seen_search_state = False
        def execute(self, sql, params=()): self.last = " ".join(sql.lower().split())
        def fetchone(self): return (0,)

    result = mod.run_once(Cursor(), apply=True, now=1000)
    assert queued == ["due"]
    assert result["queued_searches"][0]["search"]["keywords"] == "due"


def test_coverage_budget_is_clamped_and_reports_impossible_queue_target(monkeypatch):
    mod = _load("services/discovery/autonomous_discovery_v1.py", "coverage_capacity_test")
    monkeypatch.setattr(mod, "_preferences", lambda cur: {
        "location_allow_patterns": [f"City{i}" for i in range(8)],
        "allowed_work_modes": [], "allowed_employment_types": [], "freshness_days": 30,
    })
    monkeypatch.setattr(mod, "_prioritized_terms", lambda cur: [f"Role{i}" for i in range(100)])
    monkeypatch.setenv("JOBOS_PROFILE_DISCOVERY_INTERVAL_SECONDS", "900")
    monkeypatch.setenv("JOBOS_LINKEDIN_TARGET_COVERAGE_HOURS", "24")
    monkeypatch.setenv("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", "3")

    class Cursor:
        def execute(self, sql, params=()):
            pass

        def fetchone(self):
            return (0,)

    coverage = mod._coverage_requirements(Cursor())
    assert coverage["matrix_size"] == 800
    assert coverage["required_searches_per_cycle"] == 9
    assert len(mod.build_linkedin_plan(Cursor())) == 9


def test_ats_semantic_health_stays_degraded_while_source_is_backed_off(monkeypatch):
    mod = _load("services/runtime/periodic_tasks_v1.py", "ats_semantic_health_test")
    import psycopg

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=()): pass
        def fetchall(self): return [("Acme", "workday", 3, "permanent", "tomorrow")]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cur()

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    ok, detail = mod._ats_source_health()
    assert ok is False
    assert "Acme(workday)" in detail


def test_status_surfaces_ats_source_health_not_only_wrapper_health():
    jobos = (ROOT / "scripts/jobos.py").read_text()
    supervisor = (ROOT / "scripts/jobos_runtime_supervisor.py").read_text()
    periodic = (ROOT / "services/runtime/periodic_tasks_v1.py").read_text()
    assert "ats_source_failures" in jobos
    assert "ats_source_health" in supervisor
    assert 'state["ready"] = False' in supervisor
    assert "_ats_source_health() if task == \"ats-discovery\"" in periodic


def test_no_unbounded_raw_numeric_env_parsers_remain_outside_safe_helpers():
    offenders: list[str] = []
    allowed_helpers = {
        "services/discovery/ats_discovery_v1.py",
        "services/discovery/autonomous_discovery_v1.py",
        "services/runtime/periodic_tasks_v1.py",
        # Embedding dimension is a persisted/vector-shape authority.  A typo
        # must fail fast rather than silently substituting another dimension.
        "services/profile-ingestion/embed_profile_chunks.py",
        "services/profile-ingestion/profile_retrieval_api.py",
    }
    for path in (ROOT / "services").rglob("*.py"):
        text = path.read_text(errors="ignore")
        if "int(os.getenv" in text or "float(os.getenv" in text:
            rel = str(path.relative_to(ROOT))
            if rel not in allowed_helpers:
                offenders.append(rel)
    assert offenders == []
