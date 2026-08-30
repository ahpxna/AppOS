#!/usr/bin/env python3
"""Small local supervisor behind `jobos start|status|stop`.

Daily users should interact through Telegram/UI.  This supervisor hides the
Python worker topology and restarts configured local workers if one exits.
It deliberately does not bypass readiness/approval gates; it only keeps the
existing workers alive.
"""
from __future__ import annotations

import argparse
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
from services.common.config import database_dsn, load_repo_env
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER

RUN_DIR = ROOT / ".jobos" / "run"
LOG_DIR = ROOT / ".jobos" / "logs"
SUPERVISOR_PID = RUN_DIR / "supervisor.pid"
STATE_FILE = RUN_DIR / "runtime.json"
STOP = False
MAX_LOG_BYTES = 10 * 1024 * 1024


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


def _read_pid() -> int | None:
    try:
        return int(SUPERVISOR_PID.read_text().strip())
    except Exception:
        return None


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
             "--interval-seconds", os.getenv("JOBOS_ATS_POLL_INTERVAL_SECONDS", "900")),
            required=False,
        ),
        "profile-discovery": WorkerSpec(
            "profile-discovery",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "profile-discovery",
             "--interval-seconds", os.getenv("JOBOS_PROFILE_DISCOVERY_INTERVAL_SECONDS", "900")),
            required=False,
        ),
    }
    if (os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")) and (
        os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID")
    ):
        specs["telegram"] = WorkerSpec("telegram", (sys.executable, "-m", "services.telegram.telegram_review_bot_v1"))
    if os.getenv("JOBOS_GMAIL_ACCOUNT") or os.getenv("GMAIL_ACCOUNT"):
        specs["gmail-watcher"] = WorkerSpec("gmail-watcher", (sys.executable, "-m", "services.auth.gmail_verification_watcher_v1", "--interval-seconds", "10"))
    if _truthy("JOBOS_REPO_FRESHNESS_WATCH_ENABLED", False):
        specs["repo-freshness"] = WorkerSpec(
            "repo-freshness",
            (sys.executable, "-m", "services.runtime.periodic_tasks_v1", "repo-freshness",
             "--interval-seconds", os.getenv("JOBOS_REPO_FRESHNESS_INTERVAL_SECONDS", "3600")),
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
                          restarts: dict[str, int], specs: dict[str, WorkerSpec], degraded: dict[str, str]) -> None:
    try:
        import psycopg
        with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE runtime_instances SET status=%s,heartbeat_at=now() WHERE id=%s::uuid;""",
                ("degraded" if degraded else "running", runtime_instance_id),
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
    except Exception:
        pass


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
                 runtime_instance_id: str | None = None) -> None:
    data = {
        "supervisor_pid": os.getpid(),
        "runtime_instance_id": runtime_instance_id,
        "updated_at_unix": int(time.time()),
        "expected_required_services": sorted(name for name, spec in specs.items() if spec.required),
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
                                start_new_session=False, close_fds=True)
    finally:
        # The child inherited its descriptor.  Keeping a parent descriptor open
        # on each restart eventually leaks FDs in a long-running daily runtime.
        stream.close()


def _shutdown(children: dict[str, subprocess.Popen[Any]]) -> None:
    for proc in children.values():
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    deadline = time.time() + 8
    while time.time() < deadline and any(proc.poll() is None for proc in children.values()):
        time.sleep(0.2)
    for proc in children.values():
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def daemon() -> int:
    global STOP
    load_repo_env()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
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
    try:
        while not STOP:
            specs = _specs()
            for stale in set(children) - set(specs):
                proc = children.pop(stale)
                degraded.pop(stale, None)
                restart_times.pop(stale, None)
                if proc.poll() is None:
                    proc.terminate()
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
            _write_state(children, restarts, specs, degraded, runtime_instance_id)
            _db_runtime_heartbeat(runtime_instance_id, children, restarts, specs, degraded)
            time.sleep(2)
    finally:
        _shutdown(children)
        _db_runtime_stop(runtime_instance_id)
        try:
            STATE_FILE.unlink(missing_ok=True)
            SUPERVISOR_PID.unlink(missing_ok=True)
        except Exception:
            pass
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


def start() -> int:
    load_repo_env()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pid = _read_pid()
    if _alive(pid):
        print(f"JobOS is already running (supervisor pid {pid}).")
        return 0
    SUPERVISOR_PID.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    _start_infra()
    log = (LOG_DIR / "supervisor.log").open("ab", buffering=0)
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "daemon"], cwd=ROOT,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        log.close()
    expected = {name for name, spec in _specs().items() if spec.required}
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
        services = state.get("services") if isinstance(state.get("services"), dict) else {}
        children_ready = bool(expected) and all(
            bool(isinstance(services.get(name), dict) and services[name].get("running"))
            for name in expected
        )
        if _alive(proc.pid) and SUPERVISOR_PID.exists() and children_ready:
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
    running = _alive(pid)
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
    state["ready"] = running and state["state_fresh"] and not required_failures
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
            if unhealthy:
                state["ready"] = False
                state["periodic_failures"] = [entry["task"] for entry in unhealthy]
    except Exception as exc:
        state["database_runtime"]={"available":False,"error":str(exc)[:300]}
    print(json.dumps(state, indent=2, default=str))
    return 0 if state["ready"] else 1


def stop() -> int:
    pid = _read_pid()
    if not _alive(pid):
        SUPERVISOR_PID.unlink(missing_ok=True)
        STATE_FILE.unlink(missing_ok=True)
        print("JobOS is not running.")
        return 0
    os.kill(int(pid), signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
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
