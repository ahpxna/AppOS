from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from services.ats.contracts import WorkMode, infer_work_mode
from services.common.config import env_float, env_int
from services.common.search_preferences import preference_reason
from services.intake.manual_job_intake import JobDraft, ManualIntakeError, normalize_draft

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


def test_manual_intake_uses_canonical_work_mode_authority():
    base = dict(company="Acme", job_title="Engineer", jd_text="x" * 250)
    assert normalize_draft(JobDraft(**base, work_mode="On-site")).work_mode == "on_site"
    assert normalize_draft(JobDraft(**base, work_mode="on_site")).work_mode == "on_site"
    with pytest.raises(ManualIntakeError):
        normalize_draft(JobDraft(**base, work_mode="sometimes_remote"))


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

    def plan(cur, *, now=None, scan_all=False):
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
    monkeypatch.setattr(mod, "approved_terms", lambda cur: ["cooldown", "due"])
    monkeypatch.setattr(mod, "_preferences", lambda cur: {
        "location_allow_patterns": [], "allowed_work_modes": [],
        "allowed_employment_types": [], "freshness_days": 30,
    })
    monkeypatch.setattr(mod, "_prioritized_terms", lambda cur: ["cooldown", "due"])
    monkeypatch.setenv("JOBOS_LINKEDIN_SEARCHES_PER_CYCLE", "1")
    monkeypatch.setattr(mod, "_coverage_requirements", lambda cur: {
        "matrix_size": 2, "target_hours": 24, "cycles_in_target": 96,
        "required_searches_per_cycle": 1,
    })
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


def test_supervisor_state_never_reports_ready_when_db_heartbeat_failed(monkeypatch, tmp_path):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_db_ready_test")
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "runtime.json")

    class Process:
        pid = 123
        def poll(self): return None

    spec = mod.WorkerSpec("required", ("python",), required=True)
    children = {"required": Process()}
    specs = {"required": spec}
    mod._write_state(
        children, {}, specs, {}, "00000000-0000-0000-0000-000000000001",
        database_ok=False, database_error="connection refused",
    )
    state = json.loads(mod.STATE_FILE.read_text())
    assert state["ready"] is False
    assert state["database_health"]["available"] is False

    mod._write_state(
        children, {}, specs, {}, "00000000-0000-0000-0000-000000000001",
        database_ok=True, browser_ok=True,
    )
    assert json.loads(mod.STATE_FILE.read_text())["ready"] is True


def test_supervisor_operational_intervals_are_bounded_and_typo_safe(monkeypatch):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_safe_intervals_test")
    monkeypatch.setenv("JOBOS_ATS_POLL_INTERVAL_SECONDS", "not-a-number")
    monkeypatch.setenv("JOBOS_PROFILE_DISCOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("JOBOS_AUTONOMOUS_DISCOVERY_ENABLED", "true")
    specs = mod._specs()
    assert specs["ats-discovery"].argv[-1] == "900"
    assert specs["profile-discovery"].argv[-1] == "60"


def test_supervisor_can_own_native_openclaw_gateway(monkeypatch):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_native_openclaw_test")
    monkeypatch.setenv("JOBOS_RUNTIME_MANAGE_NATIVE_OPENCLAW", "1")
    specs = mod._specs()
    gateway = specs["openclaw-gateway"]
    assert gateway.required is True
    assert gateway.argv[-2:] == (
        str(ROOT / "scripts" / "start_openclaw_jobos.py"),
        "gateway",
    )


def test_supervisor_heartbeat_recovers_missing_parent_and_surfaces_degraded_health(monkeypatch):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_heartbeat_recovery_test")
    import psycopg
    statements: list[str] = []

    class Cursor:
        rows = []
        one = None
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            statements.append(normalized)
            if "from schema_migrations" in normalized:
                from scripts.apply_migrations import checksum, migration_files
                self.one = (checksum(migration_files()[-1]),)
                self.rows = []
            elif "from periodic_task_health" in normalized:
                self.rows = [("ats-discovery",)]
                self.one = None
            elif "from ats_companies" in normalized:
                self.rows = [("source-1",)]
                self.one = None
            else:
                self.rows = []
                self.one = None
        def fetchone(self): return self.one
        def fetchall(self): return list(self.rows)

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cursor()
        def commit(self): pass

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())

    class Process:
        pid = 123
        def poll(self): return None

    spec = mod.WorkerSpec("required", ("python",), required=True)
    available, health_error = mod._db_runtime_heartbeat(
        "00000000-0000-0000-0000-000000000001",
        {"required": Process()}, {}, {"required": spec}, {},
    )
    assert available is True
    assert health_error == "periodic=ats-discovery; ats_sources=source-1"
    assert any("insert into runtime_instances" in sql and "on conflict (id) do update" in sql
               for sql in statements)


def test_supervisor_readiness_fails_closed_on_missing_latest_migration(monkeypatch):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_migration_ready_test")
    import psycopg

    class Cursor:
        rows = []
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=()): self.rows = []
        def fetchone(self): return None
        def fetchall(self): return list(self.rows)

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cursor()
        def commit(self): pass

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())
    available, health_error = mod._db_runtime_heartbeat(
        "00000000-0000-0000-0000-000000000001", {}, {}, {}, {},
    )
    from scripts.apply_migrations import migration_files
    latest = migration_files()[-1].name
    assert available is True
    assert health_error and f"migration_not_current={latest}" in health_error


def test_supervisor_status_preserves_heartbeat_migration_failure(monkeypatch, tmp_path, capsys):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_status_migration_test")
    import psycopg

    state_file = tmp_path / "runtime.json"
    state_file.write_text(json.dumps({
        "updated_at_unix": int(mod.time.time()),
        "ready": False,
        "database_health": {
            "available": True,
            "error": "migration_not_current=100_remove_legacy_production_mock_seeds.sql",
        },
        "services": {"required": {"running": True, "required": True}},
    }))
    monkeypatch.setattr(mod, "STATE_FILE", state_file)
    monkeypatch.setattr(mod, "_read_pid", lambda: 123)
    monkeypatch.setattr(mod, "_is_supervisor_process", lambda pid: True)
    monkeypatch.setattr(
        mod, "_specs",
        lambda: {"required": mod.WorkerSpec("required", ("python",), required=True)},
    )

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=()): self.sql = sql
        def fetchone(self): return None
        def fetchall(self): return []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cursor()

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())
    assert mod.status() == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is False
    assert output["database_health"]["error"].startswith("migration_not_current=")


def test_jobos_status_preserves_heartbeat_migration_failure(monkeypatch, tmp_path, capsys):
    mod = _load("scripts/jobos.py", "jobos_status_migration_test")
    supervisor = __import__("scripts.jobos_runtime_supervisor", fromlist=["dummy"])
    import psycopg

    run_dir = tmp_path / ".jobos" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "supervisor.pid").write_text("123")
    (run_dir / "runtime.json").write_text(json.dumps({
        "supervisor_pid": 123,
        "updated_at_unix": int(mod.time.time()),
        "ready": False,
        "database_health": {
            "available": True,
            "error": "migration_not_current=100_remove_legacy_production_mock_seeds.sql",
        },
        "expected_required_services": ["required"],
        "services": {"required": {"running": True, "required": True}},
    }))
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "load_repo_env", lambda: None)
    monkeypatch.setattr(supervisor, "_is_supervisor_process", lambda pid: True)

    class Cursor:
        row = None
        rows: list[tuple] = []
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.row = (0,) if "select count(*)" in normalized else None
            self.rows = []
        def fetchone(self): return self.row
        def fetchall(self): return list(self.rows)

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cursor()

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())
    assert mod.status() == 1
    output = json.loads(capsys.readouterr().out)
    assert output["runtime"]["ready"] is False
    assert output["runtime"]["database_health"]["error"].startswith("migration_not_current=")


def test_supervisor_pid_identity_rejects_reused_unrelated_pid(monkeypatch):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_pid_identity_test")
    from types import SimpleNamespace
    monkeypatch.setattr(mod, "_alive", lambda pid: True)
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="/usr/bin/python unrelated_worker.py"),
    )
    assert mod._is_supervisor_process(123) is False
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="/venv/bin/python /repo/scripts/jobos_runtime_supervisor.py daemon"
        ),
    )
    assert mod._is_supervisor_process(123) is True


def test_supervisor_stop_never_signals_unproven_stale_pid(monkeypatch, tmp_path):
    mod = _load("scripts/jobos_runtime_supervisor.py", "supervisor_safe_stop_test")
    pid_file = tmp_path / "supervisor.pid"
    state_file = tmp_path / "runtime.json"
    pid_file.write_text("999")
    state_file.write_text("{}")
    monkeypatch.setattr(mod, "SUPERVISOR_PID", pid_file)
    monkeypatch.setattr(mod, "STATE_FILE", state_file)
    monkeypatch.setattr(mod, "_is_supervisor_process", lambda pid: False)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(mod.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert mod.stop() == 0
    assert signals == []
    assert not pid_file.exists() and not state_file.exists()
