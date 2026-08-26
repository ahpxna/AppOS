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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.common.config import load_repo_env

RUN_DIR = ROOT / ".jobos" / "run"
LOG_DIR = ROOT / ".jobos" / "logs"
SUPERVISOR_PID = RUN_DIR / "supervisor.pid"
STATE_FILE = RUN_DIR / "runtime.json"
STOP = False


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


def _specs() -> dict[str, list[str]]:
    specs: dict[str, list[str]] = {
        "orchestrator": [sys.executable, "-m", "services.orchestrator.orchestrator_worker_v1", "--poll-seconds", "15"],
        "privileged-actions": [sys.executable, "-m", "services.application_actions.privileged_action_v1", "worker", "--poll-seconds", "5"],
        "browser-worker": [sys.executable, str(ROOT / "services" / "browser-controller" / "browser_queue_worker.py"), "--poll-seconds", "5"],
    }
    if (os.getenv("JOBOS_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")) and (
        os.getenv("JOBOS_TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID")
    ):
        specs["telegram"] = [sys.executable, "-m", "services.telegram.telegram_review_bot_v1"]
    if os.getenv("JOBOS_GMAIL_ACCOUNT") or os.getenv("GMAIL_ACCOUNT"):
        specs["gmail-watcher"] = [sys.executable, "-m", "services.auth.gmail_verification_watcher_v1", "--interval-seconds", "10"]
    return specs


def _write_state(children: dict[str, subprocess.Popen[Any]], restarts: dict[str, int]) -> None:
    data = {
        "supervisor_pid": os.getpid(),
        "updated_at_unix": int(time.time()),
        "services": {
            name: {"pid": proc.pid, "running": proc.poll() is None, "restarts": restarts.get(name, 0),
                   "log": str(LOG_DIR / f"{name}.log")}
            for name, proc in children.items()
        },
    }
    STATE_FILE.write_text(json.dumps(data, indent=2))


def _spawn(name: str, argv: list[str]) -> subprocess.Popen[Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stream = (LOG_DIR / f"{name}.log").open("ab", buffering=0)
    try:
        return subprocess.Popen(argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
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
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("STOP", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("STOP", True))
    children: dict[str, subprocess.Popen[Any]] = {}
    restarts: dict[str, int] = {}
    last_start: dict[str, float] = {}
    try:
        while not STOP:
            specs = _specs()
            for stale in set(children) - set(specs):
                proc = children.pop(stale)
                if proc.poll() is None:
                    proc.terminate()
            for name, argv in specs.items():
                proc = children.get(name)
                if proc is None or proc.poll() is not None:
                    # Avoid a tight crash loop while still self-healing.
                    if time.time() - last_start.get(name, 0) < 5:
                        continue
                    children[name] = _spawn(name, argv)
                    last_start[name] = time.time()
                    restarts[name] = restarts.get(name, 0) + (1 if proc is not None else 0)
            _write_state(children, restarts)
            time.sleep(2)
    finally:
        _shutdown(children)
        try:
            STATE_FILE.unlink(missing_ok=True)
            SUPERVISOR_PID.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def _start_infra() -> None:
    if not _truthy("JOBOS_RUNTIME_START_POSTGRES", True):
        return
    docker = shutil.which("docker")
    if not docker:
        return
    subprocess.run([docker, "compose", "up", "-d", "postgres"], cwd=ROOT, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if _truthy("JOBOS_RUNTIME_START_OPENCLAW", False):
        subprocess.run([docker, "compose", "-f", "docker-compose.openclaw.yml", "up", "-d", "openclaw", "browser"],
                       cwd=ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start() -> int:
    load_repo_env()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pid = _read_pid()
    if _alive(pid):
        print(f"JobOS is already running (supervisor pid {pid}).")
        return 0
    _start_infra()
    log = (LOG_DIR / "supervisor.log").open("ab", buffering=0)
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "daemon"], cwd=ROOT,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        log.close()
    expected = set(_specs())
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
    print(json.dumps(state, indent=2))
    return 0 if running else 1


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
