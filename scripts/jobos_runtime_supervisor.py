#!/usr/bin/env python3
"""Small local supervisor behind `jobos start|status|stop`.

Daily users should interact through Telegram/UI.  This supervisor hides the
Python worker topology and restarts configured local workers if one exits.
It deliberately does not bypass readiness/approval gates; it only keeps the
existing workers alive.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, env_int, load_repo_env
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER

RUN_DIR = ROOT / ".jobos" / "run"
LOG_DIR = ROOT / ".jobos" / "logs"
SUPERVISOR_PID = RUN_DIR / "supervisor.pid"
STATE_FILE = RUN_DIR / "runtime.json"
SUPERVISOR_LOCK = RUN_DIR / "supervisor.lock"
STOP = False
MAX_LOG_BYTES = 10 * 1024 * 1024
BROWSER_HEALTH_INTERVAL_SECONDS = env_int(
    "JOBOS_RUNTIME_BROWSER_HEALTH_INTERVAL_SECONDS", 15, minimum=5, maximum=120
)


@dataclass(frozen=True)
class RestartPolicy:
    initial_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 60.0
    max_restarts: int = 5
    window_seconds: float = 300.0


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    argv: tuple[str, ...]
    required: bool = True
    restart: RestartPolicy = RestartPolicy()


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_supervisor_process(pid: int | None) -> bool:
    """Prove a persisted PID still belongs to this supervisor before trust/kill.

    PID reuse is normal after a crash or reboot. A stale file must never make
    ``status`` report an unrelated process as JobOS or let ``stop`` signal it.
    """
    if not _alive(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            text=True, capture_output=True, check=False, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
    command = result.stdout.strip()
    return result.returncode == 0 and "jobos_runtime_supervisor.py" in command and "daemon" in command


def _read_pid() -> int | None:
    try:
        return int(SUPERVISOR_PID.read_text().strip())
    except Exception:
        return None


def runtime_state_ready(
    state: dict[str, Any], *, running: bool, state_fresh: bool,
    required_failures: list[str] | tuple[str, ...] | set[str],
) -> bool:
    """Return fail-closed readiness from the persisted heartbeat authority.

    PID and worker liveness cannot override the database/migration verdict
    written by the heartbeat.  Missing or malformed database health is also
    non-ready so an old runtime.json format cannot silently fail open.
    """
    database_health = state.get("database_health")
    database_ready = bool(
        isinstance(database_health, dict)
        and database_health.get("available") is True
        and not database_health.get("error")
    )
    browser_health = state.get("browser_runtime_health")
    browser_ready = bool(
        isinstance(browser_health, dict)
        and browser_health.get("available") is True
        and not browser_health.get("error")
    )
    return bool(running and state_fresh and not required_failures and database_ready and browser_ready)


def _browser_runtime_health() -> tuple[bool, str | None]:
    """Probe gateway RPC + CDP without reading tabs or invoking a model.

    A live browser-worker process proves only its polling loop is alive. It can
    remain alive indefinitely while the native OpenClaw gateway or Chrome CDP
    listener is absent, so process liveness alone must not produce READY.
    """
    result = DEFAULT_PROCESS_RUNNER.run(
        [sys.executable, str(ROOT / "services" / "browser-controller" / "browser_queue_worker.py"), "--health"],
        cwd=ROOT, timeout_s=15,
    )
    if result.ok:
        return True, None
    detail = (result.output or result.start_error or "Gateway/CDP health probe failed").strip()
    return False, detail[-1000:]


def _specs() -> dict[str, WorkerSpec]:
    specs: dict[str, WorkerSpec] = {
        "orchestrator": WorkerSpec("orchestrator", (sys.executable, "-m", "services.orchestrator.orchestrator_worker_v1", "--poll-seconds", "15")),
        "privileged-actions": WorkerSpec("privileged-actions", (sys.executable, "-m", "services.application_actions.privileged_action_v1", "worker", "--poll-seconds", "5")),
        "browser-worker": WorkerSpec("browser-worker", (sys.executable, str(ROOT / "services" / "browser-controller" / "browser_queue_worker.py"), "--poll-seconds", "5")),
        "browser-state-watcher": WorkerSpec("browser-state-watcher", (sys.executable, "-m", "services.auth.browser_state_watcher_v1", "--poll-seconds", "5")),
        "document-revision": WorkerSpec("document-revision", (sys.executable, "-m", "services.review.document_revision_worker_v1", "--poll-seconds", "5")),
        # Public ATS discovery is autonomous from application progression, but
        # its lifecycle belongs to the same supervisor rather than a forgotten
        # cron/manual command. Empty company registries simply produce a no-op.
        "ats-discovery": WorkerSpec(
            "ats-discovery",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "ats-discovery",
             "--interval-seconds", str(env_int(
                 "JOBOS_ATS_POLL_INTERVAL_SECONDS", 900, minimum=60, maximum=86400
             ))),
            required=False,
        ),
    }
    # Native OpenClaw must have the same lifecycle owner as the workers that
    # depend on it. Opt-in preserves Docker/external gateway deployments, but
    # when enabled no terminal or ad-hoc nohup process is part of readiness.
    if _truthy("JOBOS_RUNTIME_MANAGE_NATIVE_OPENCLAW", False):
        specs["openclaw-gateway"] = WorkerSpec(
            "openclaw-gateway",
            (sys.executable, str(ROOT / "scripts" / "start_openclaw_jobos.py"), "gateway"),
        )
    # Capability flags permit a manually requested LinkedIn read; only the
    # separate autonomous opt-in is allowed to start a periodic scheduler.
    if _truthy("JOBOS_AUTONOMOUS_DISCOVERY_ENABLED", False):
        specs["profile-discovery"] = WorkerSpec(
            "profile-discovery",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "profile-discovery",
             "--interval-seconds", str(env_int(
                 "JOBOS_PROFILE_DISCOVERY_INTERVAL_SECONDS", 900, minimum=60, maximum=86400
             ))),
            required=False,
        )
    if (os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")) and (
        os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID")
    ):
        specs["telegram"] = WorkerSpec("telegram", (sys.executable, "-m", "services.telegram.telegram_review_bot_v1"))
    if os.getenv("JOBOS_GMAIL_ACCOUNT") or os.getenv("GMAIL_ACCOUNT"):
        specs["gmail-watcher"] = WorkerSpec(
            "gmail-watcher",
            (sys.executable, "-m", "services.auth.gmail_verification_watcher_v1",
             "--wake-listen", "--interval-seconds", "10"),
        )
    if _truthy("JOBOS_REPO_FRESHNESS_WATCH_ENABLED", False):
        specs["repo-freshness"] = WorkerSpec(
            "repo-freshness",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "repo-freshness",
             "--interval-seconds", str(env_int(
                 "JOBOS_REPO_FRESHNESS_INTERVAL_SECONDS", 3600, minimum=60, maximum=86400
             ))),
            required=False,
        )
    if _truthy("JOBOS_MESSAGE_WORKER_ENABLED", True):
        specs["message-pipeline"] = WorkerSpec(
            "message-pipeline",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "message-pipeline",
             "--interval-seconds", str(env_int(
                 "JOBOS_MESSAGE_WORKER_INTERVAL_SECONDS", 60, minimum=60, maximum=86400
             ))),
            required=False,
        )
    if _truthy("JOBOS_INTERVIEW_PREP_WORKER_ENABLED", True):
        specs["interview-prep"] = WorkerSpec(
            "interview-prep",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "interview-prep",
             "--interval-seconds", str(env_int(
                 "JOBOS_INTERVIEW_PREP_INTERVAL_SECONDS", 300, minimum=60, maximum=86400
             ))),
            required=False,
        )
    return specs


def _db_runtime_start(runtime_instance_id: str) -> None:
    try:
        import psycopg
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO runtime_instances(id,hostname,pid,release_version,git_commit,status,started_at,heartbeat_at)
                   VALUES (%s::uuid,%s,%s,%s,%s,'running',now(),now())
                   ON CONFLICT (id) DO UPDATE SET status='running',heartbeat_at=now();""",
                (runtime_instance_id,socket.gethostname(),os.getpid(),os.getenv("JOBOS_RELEASE_VERSION"),
                 os.getenv("JOBOS_GIT_COMMIT")),
            )
    except Exception:
        # Runtime DB history is durable telemetry; OS PID/process state remains
        # the immediate liveness authority and must still work during DB repair.
        pass


def _db_runtime_heartbeat(runtime_instance_id: str, children: dict[str, subprocess.Popen[Any]],
                          restarts: dict[str, int], specs: dict[str, WorkerSpec],
                          degraded: dict[str, str]) -> tuple[bool, str | None]:
    try:
        import psycopg
        with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
            from scripts.apply_migrations import checksum, migration_files
            latest_path = migration_files()[-1]
            cur.execute(
                "SELECT checksum_sha256 FROM schema_migrations WHERE migration_id=%s;",
                (latest_path.name,),
            )
            migration_row = cur.fetchone()
            migration_current = bool(migration_row and str(migration_row[0]) == checksum(latest_path))
            cur.execute(
                """SELECT task_key FROM periodic_task_health
                    WHERE consecutive_failures>0 ORDER BY task_key LIMIT 20;"""
            )
            periodic_failures = [str(row[0]) for row in cur.fetchall()]
            cur.execute(
                """SELECT id::text FROM ats_companies
                    WHERE enabled=true AND consecutive_failures>0 ORDER BY id LIMIT 20;"""
            )
            ats_failures = [str(row[0]) for row in cur.fetchall()]
            cur.execute(
                """SELECT count(*) FROM llm_calls
                    WHERE status='uncertain'
                       OR (status='running' AND started_at < now()-interval '30 minutes');"""
            )
            llm_attention = int((cur.fetchone() or (0,))[0] or 0)
            cur.execute(
                """SELECT count(*) FROM llm_cost_reservations
                    WHERE status='reserved' AND created_at < now()-interval '30 minutes';"""
            )
            stale_reservations = int((cur.fetchone() or (0,))[0] or 0)
            health_failures: list[str] = []
            if not migration_current:
                health_failures.append("migration_not_current=" + latest_path.name)
            if periodic_failures:
                health_failures.append("periodic=" + ",".join(periodic_failures))
            if ats_failures:
                health_failures.append("ats_sources=" + ",".join(ats_failures))
            if llm_attention:
                health_failures.append(f"llm_calls_needing_reconciliation={llm_attention}")
            if stale_reservations:
                health_failures.append(f"stale_llm_reservations={stale_reservations}")
            health_error = "; ".join(health_failures) or None
            runtime_status = "degraded" if degraded or health_error else "running"
            # This is deliberately an UPSERT rather than UPDATE. If PostgreSQL
            # was unavailable during daemon startup, the first successful
            # heartbeat recreates the parent row before runtime_services' FK
            # writes and lets readiness recover without a manual restart.
            cur.execute(
                """INSERT INTO runtime_instances
                     (id,hostname,pid,release_version,git_commit,status,started_at,heartbeat_at)
                   VALUES (%s::uuid,%s,%s,%s,%s,%s,now(),now())
                   ON CONFLICT (id) DO UPDATE SET
                     hostname=EXCLUDED.hostname,pid=EXCLUDED.pid,
                     release_version=EXCLUDED.release_version,git_commit=EXCLUDED.git_commit,
                     status=EXCLUDED.status,heartbeat_at=now(),stopped_at=NULL;""",
                (runtime_instance_id, socket.gethostname(), os.getpid(),
                 os.getenv("JOBOS_RELEASE_VERSION"), os.getenv("JOBOS_GIT_COMMIT"), runtime_status),
            )
            for name,spec in specs.items():
                proc=children.get(name)
                running=bool(proc is not None and proc.poll() is None)
                cur.execute(
                    """INSERT INTO runtime_services(runtime_instance_id,service_key,pid,required,status,restart_count,heartbeat_at,last_error)
                       VALUES (%s::uuid,%s,%s,%s,%s,%s,now(),%s)
                       ON CONFLICT (runtime_instance_id,service_key) DO UPDATE SET
                         pid=EXCLUDED.pid,required=EXCLUDED.required,status=EXCLUDED.status,
                         restart_count=EXCLUDED.restart_count,heartbeat_at=now(),last_error=EXCLUDED.last_error;""",
                    (runtime_instance_id,name,proc.pid if proc else None,spec.required,
                     "running" if running else ("degraded" if name in degraded else "stopped"),
                     restarts.get(name,0),degraded.get(name)),
                )
            conn.commit()
        return True, health_error
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]


def _db_runtime_stop(runtime_instance_id: str) -> None:
    try:
        import psycopg
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE runtime_instances SET status='stopped',heartbeat_at=now(),stopped_at=now()
                    WHERE id=%s::uuid;""", (runtime_instance_id,),
            )
            cur.execute(
                """UPDATE runtime_services SET status='stopped',heartbeat_at=now()
                    WHERE runtime_instance_id=%s::uuid;""", (runtime_instance_id,),
            )
    except Exception:
        pass


def _write_state(children: dict[str, subprocess.Popen[Any]], restarts: dict[str, int],
                 specs: dict[str, WorkerSpec], degraded: dict[str, str],
                 runtime_instance_id: str | None = None, *, database_ok: bool = False,
                 database_error: str | None = None, browser_ok: bool = False,
                 browser_error: str | None = None) -> None:
    required_running = all(
        name in children and children[name].poll() is None
        for name, spec in specs.items() if spec.required
    )
    data = {
        "supervisor_pid": os.getpid(),
        "runtime_instance_id": runtime_instance_id,
        "updated_at_unix": int(time.time()),
        "expected_required_services": sorted(name for name, spec in specs.items() if spec.required),
        "ready": bool(required_running and database_ok and not database_error
                      and browser_ok and not browser_error),
        "database_health": {"available": bool(database_ok), "error": database_error},
        "browser_runtime_health": {"available": bool(browser_ok), "error": browser_error},
        "services": {
            name: {
                "pid": children[name].pid if name in children else None,
                "running": bool(name in children and children[name].poll() is None),
                "required": spec.required, "restarts": restarts.get(name, 0),
                "degraded": degraded.get(name), "log": str(LOG_DIR / f"{name}.log"),
            }
            for name, spec in specs.items()
        },
    }
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2))
    os.replace(temporary, STATE_FILE)


def _rotate_log(name: str) -> Path:
    path = LOG_DIR / f"{name}.log"
    if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
        previous = path.with_suffix(".log.1")
        previous.unlink(missing_ok=True)
        os.replace(path, previous)
    return path


def _spawn(spec: WorkerSpec) -> subprocess.Popen[Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stream = _rotate_log(spec.name).open("ab", buffering=0)
    try:
        return subprocess.Popen(spec.argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    finally:
        # The child inherited its descriptor.  Keeping a parent descriptor open
        # on each restart eventually leaks FDs in a long-running daily runtime.
        stream.close()


def _shutdown(children: dict[str, subprocess.Popen[Any]]) -> None:
    for proc in children.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
    deadline = time.time() + 8
    while time.time() < deadline and any(proc.poll() is None for proc in children.values()):
        time.sleep(0.2)
    for proc in children.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass


def daemon() -> int:
    global STOP
    load_repo_env()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_stream = SUPERVISOR_LOCK.open("a+")
    try:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_stream.close()
        print("Another JobOS supervisor already owns the runtime lock.", file=sys.stderr)
        return 1
    SUPERVISOR_PID.write_text(str(os.getpid()))
    runtime_instance_id = str(uuid.uuid4())
    _db_runtime_start(runtime_instance_id)
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("STOP", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("STOP", True))
    children: dict[str, subprocess.Popen[Any]] = {}
    restarts: dict[str, int] = {}
    last_start: dict[str, float] = {}
    restart_times: dict[str, list[float]] = {}
    degraded: dict[str, str] = {}
    browser_ok = False
    browser_error: str | None = "browser runtime has not been probed yet"
    last_browser_probe = 0.0
    try:
        while not STOP:
            specs = _specs()
            for stale in set(children) - set(specs):
                proc = children.pop(stale)
                degraded.pop(stale, None)
                restart_times.pop(stale, None)
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for name, spec in specs.items():
                proc = children.get(name)
                if proc is None or proc.poll() is not None:
                    now = time.time()
                    history = [at for at in restart_times.get(name, []) if now - at <= spec.restart.window_seconds]
                    restart_times[name] = history
                    if name in degraded:
                        # Circuit breakers are time-windowed, not permanent. Once
                        # enough restart timestamps age out of the configured
                        # window, automatically close the circuit and try again.
                        if len(history) < spec.restart.max_restarts:
                            degraded.pop(name, None)
                        else:
                            continue
                    if proc is not None:
                        delay = min(
                            spec.restart.max_backoff_seconds,
                            spec.restart.initial_backoff_seconds * (2 ** max(0, len(history) - 1)),
                        )
                        if now - last_start.get(name, 0) < delay:
                            continue
                        if len(history) >= spec.restart.max_restarts:
                            degraded[name] = (
                                f"restart circuit open after {len(history)} failures in "
                                f"{int(spec.restart.window_seconds)}s"
                            )
                            continue
                        history.append(now)
                        restarts[name] = restarts.get(name, 0) + 1
                    children[name] = _spawn(spec)
                    last_start[name] = now
            database_ok, database_error = _db_runtime_heartbeat(
                runtime_instance_id, children, restarts, specs, degraded
            )
            monotonic_now = time.monotonic()
            if monotonic_now - last_browser_probe >= BROWSER_HEALTH_INTERVAL_SECONDS:
                browser_ok, browser_error = _browser_runtime_health()
                last_browser_probe = monotonic_now
            _write_state(
                children, restarts, specs, degraded, runtime_instance_id,
                database_ok=database_ok, database_error=database_error,
                browser_ok=browser_ok, browser_error=browser_error,
            )
            time.sleep(2)
    finally:
        _shutdown(children)
        _db_runtime_stop(runtime_instance_id)
        try:
            STATE_FILE.unlink(missing_ok=True)
            SUPERVISOR_PID.unlink(missing_ok=True)
        except Exception:
            pass
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()
    return 0


def _start_infra() -> None:
    docker = shutil.which("docker")
    if not docker:
        return
    if _truthy("JOBOS_RUNTIME_START_POSTGRES", True):
        DEFAULT_PROCESS_RUNNER.run([docker, "compose", "up", "-d", "postgres"], cwd=ROOT, timeout_s=120)
    if _truthy("JOBOS_RUNTIME_START_OPENCLAW", False):
        DEFAULT_PROCESS_RUNNER.run([docker, "compose", "-f", "docker-compose.openclaw.yml", "up", "-d", "openclaw", "browser"],
                                   cwd=ROOT, timeout_s=120)


def _preflight_database_contract(*, timeout_seconds: int = 30) -> tuple[bool, str | None]:
    """Prove the latest migration before any worker is allowed to start.

    ``start`` may boot PostgreSQL itself, so a short bounded connection retry is
    appropriate.  Schema drift is not transient and returns immediately.  This
    is deliberately read-only: operators must run the audited migration runner
    rather than having daily startup mutate production history implicitly.
    """
    from scripts.apply_migrations import checksum, migration_files

    expected_migrations = migration_files()
    deadline = time.monotonic() + max(1, min(int(timeout_seconds), 120))
    last_error = "PostgreSQL did not become reachable."
    while True:
        try:
            import psycopg
            with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT migration_id,checksum_sha256 FROM schema_migrations;")
                applied = {str(migration_id): str(digest) for migration_id, digest in cur.fetchall()}
            for migration in expected_migrations:
                actual = applied.get(migration.name)
                if actual is None:
                    return False, f"migration_not_current={migration.name}"
                if actual != checksum(migration):
                    return False, f"migration_checksum_drift={migration.name}"
            return True, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:500]
        if time.monotonic() >= deadline:
            return False, last_error
        time.sleep(0.25)


def start() -> int:
    load_repo_env()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pid = _read_pid()
    if _is_supervisor_process(pid):
        try:
            state = json.loads(STATE_FILE.read_text())
            age = max(0, int(time.time()) - int(state.get("updated_at_unix") or 0))
        except Exception:
            state, age = {}, 10**9
        if state.get("ready") is True and age <= 15:
            print(f"JobOS is already running and ready (supervisor pid {pid}).")
            return 0
        print(
            f"JobOS supervisor pid {pid} is running but not ready. "
            "Run `python scripts/jobos.py status` and inspect .jobos/logs.",
            file=sys.stderr,
        )
        return 1
    SUPERVISOR_PID.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    _start_infra()
    database_ok, database_error = _preflight_database_contract(
        timeout_seconds=env_int("JOBOS_RUNTIME_DATABASE_STARTUP_TIMEOUT_SECONDS", 30, minimum=1, maximum=120)
    )
    if not database_ok:
        print(
            "JobOS workers were not started because the PostgreSQL migration contract is not current: "
            f"{database_error}. Run `python scripts/apply_migrations.py`, then retry `jobos start`.",
            file=sys.stderr,
        )
        return 1
    log = (LOG_DIR / "supervisor.log").open("ab", buffering=0)
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "daemon"], cwd=ROOT,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        log.close()
    expected = {name for name, spec in _specs().items() if spec.required}
    deadline = time.time() + env_int(
        "JOBOS_RUNTIME_STARTUP_TIMEOUT_SECONDS", 30, minimum=5, maximum=120
    )
    while time.time() < deadline:
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
        if _alive(proc.pid) and SUPERVISOR_PID.exists() and state.get("ready") is True:
            if "telegram" in expected:
                print("JobOS workers are running. Daily control surface: Telegram /start")
            else:
                print("JobOS workers are running, but Telegram is not configured. Complete Telegram setup before daily use.")
            return 0
        time.sleep(0.1)
    print(f"JobOS supervisor did not become ready. Check {LOG_DIR / 'supervisor.log'}", file=sys.stderr)
    return 1


def status() -> int:
    pid = _read_pid()
    running = _is_supervisor_process(pid)
    state: dict[str, Any] = {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    state["running"] = running
    state["supervisor_pid"] = pid
    services = state.get("services") if isinstance(state.get("services"), dict) else {}
    expected_required = {name for name, spec in _specs().items() if spec.required}
    now = int(time.time())
    try:
        state_age = max(0, now - int(state.get("updated_at_unix") or 0))
    except Exception:
        state_age = 10**9
    state["state_age_seconds"] = state_age
    state["state_fresh"] = bool(state.get("updated_at_unix")) and state_age <= 15
    required_failures = sorted(
        name for name in expected_required
        if not isinstance(services.get(name), dict) or not services[name].get("running")
    )
    state["expected_required_services"] = sorted(expected_required)
    state["required_failures"] = required_failures
    state["ready"] = runtime_state_ready(
        state,
        running=running,
        state_fresh=state["state_fresh"],
        required_failures=required_failures,
    )
    try:
        import psycopg
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id::text,hostname,pid,status,started_at,heartbeat_at,stopped_at
                     FROM runtime_instances ORDER BY heartbeat_at DESC LIMIT 1;"""
            )
            row=cur.fetchone()
            if row:
                state["database_runtime"]={"id":row[0],"hostname":row[1],"pid":row[2],"status":row[3],
                                           "started_at":row[4],"heartbeat_at":row[5],"stopped_at":row[6]}
            cur.execute(
                """SELECT task_key,consecutive_failures,last_success_at,last_failure_at,last_error
                     FROM periodic_task_health
                    WHERE consecutive_failures > 0
                    ORDER BY consecutive_failures DESC, last_failure_at DESC;"""
            )
            unhealthy = [
                {"task": row[0], "consecutive_failures": int(row[1] or 0),
                 "last_success_at": row[2], "last_failure_at": row[3],
                 "last_error": str(row[4] or "")[:300]}
                for row in cur.fetchall()
            ]
            state["periodic_task_health"] = unhealthy
            cur.execute(
                """SELECT id::text,company_name,ats_platform,consecutive_failures,last_error_kind,next_retry_at
                     FROM ats_companies
                    WHERE enabled=true AND consecutive_failures>0
                    ORDER BY consecutive_failures DESC,company_name;"""
            )
            ats_unhealthy = [
                {"source_id": row[0], "company": row[1], "platform": row[2],
                 "consecutive_failures": int(row[3] or 0), "last_error_kind": row[4],
                 "next_retry_at": row[5]}
                for row in cur.fetchall()
            ]
            state["ats_source_health"] = ats_unhealthy
            if unhealthy or ats_unhealthy:
                state["ready"] = False
            if unhealthy:
                state["periodic_failures"] = [entry["task"] for entry in unhealthy]
            if ats_unhealthy:
                state["ats_source_failures"] = [entry["source_id"] for entry in ats_unhealthy]
    except Exception as exc:
        # Runtime readiness is not provable without the durable DB/control-plane
        # authority.  A live supervisor process alone must never produce a
        # successful readiness result when that health read failed.
        state["database_runtime"]={"available":False,"error":str(exc)[:300]}
        state["ready"] = False
    print(json.dumps(state, indent=2, default=str))
    return 0 if state["ready"] else 1


def stop() -> int:
    pid = _read_pid()
    if not _is_supervisor_process(pid):
        SUPERVISOR_PID.unlink(missing_ok=True)
        STATE_FILE.unlink(missing_ok=True)
        print("JobOS is not running; stale runtime state was cleared without signaling another process.")
        return 0
    os.kill(int(pid), signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline and _is_supervisor_process(pid):
        time.sleep(0.2)
    if _is_supervisor_process(pid):
        os.kill(int(pid), signal.SIGKILL)
    print("JobOS stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS local runtime supervisor")
    parser.add_argument("command", choices=("start", "status", "stop", "daemon"))
    args = parser.parse_args()
    return {"start": start, "status": status, "stop": stop, "daemon": daemon}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
