#!/usr/bin/env python3
"""Bounded periodic runtime tasks that do not belong inside the orchestrator.

Each task runs one existing CLI contract through ProcessRunner, emits no shell
command, sleeps between iterations, and remains independently restartable by
the JobOS supervisor.  This keeps discovery/freshness autonomous without
creating a second orchestration state machine.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER

STOP = False

TASKS: dict[str, tuple[list[str], int]] = {
    "ats-discovery": ([sys.executable, "-m", "services.discovery.ats_discovery_v1", "poll", "--apply"], int(os.getenv("JOBOS_ATS_PERIODIC_TIMEOUT_SECONDS", "1320"))),
    "profile-discovery": ([sys.executable, "-m", "services.discovery.autonomous_discovery_v1", "run", "--apply"], 120),
    "repo-freshness": ([sys.executable, "services/repo-audit/repository_freshness_v1.py", "refresh"], 900),
}


def _record_health(task: str, *, ok: bool, detail: str = "") -> None:
    """Best-effort durable health: wrapper liveness never masks failures."""
    try:
        from services.common.config import database_dsn
        import psycopg
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO periodic_task_health(task_key,consecutive_failures,last_success_at,last_failure_at,last_error,updated_at)
                   VALUES (%s,CASE WHEN %s THEN 0 ELSE 1 END,
                           CASE WHEN %s THEN now() ELSE NULL END,
                           CASE WHEN %s THEN NULL ELSE now() END,
                           CASE WHEN %s THEN NULL ELSE %s END,now())
                   ON CONFLICT (task_key) DO UPDATE SET
                     consecutive_failures=CASE WHEN EXCLUDED.last_success_at IS NOT NULL THEN 0 ELSE periodic_task_health.consecutive_failures+1 END,
                     last_success_at=coalesce(EXCLUDED.last_success_at,periodic_task_health.last_success_at),
                     last_failure_at=coalesce(EXCLUDED.last_failure_at,periodic_task_health.last_failure_at),
                     last_error=CASE WHEN EXCLUDED.last_success_at IS NOT NULL THEN NULL ELSE EXCLUDED.last_error END,
                     updated_at=now();""",
                (task, ok, ok, ok, ok, detail[:2000]),
            )
    except Exception:
        # The process output still carries the error when DB itself is down.
        pass


def run_loop(task: str, *, interval_seconds: int, once: bool = False) -> int:
    global STOP
    if task not in TASKS:
        raise ValueError(f"unknown periodic task: {task}")
    argv, timeout_s = TASKS[task]
    interval = max(60, int(interval_seconds))
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("STOP", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("STOP", True))
    consecutive_failures = 0
    exit_threshold = max(1, min(int(os.getenv("JOBOS_PERIODIC_FAILURE_EXIT_THRESHOLD", "3")), 20))
    while not STOP:
        result = DEFAULT_PROCESS_RUNNER.run(argv, cwd=ROOT, timeout_s=timeout_s)
        if not result.ok:
            consecutive_failures += 1
            detail = (result.output or result.start_error or "unknown periodic task failure").strip()[-2000:]
            _record_health(task, ok=False, detail=detail)
            print(f"[{task}] iteration failed (transient={result.transient}): {detail}", file=sys.stderr, flush=True)
            if not once and consecutive_failures >= exit_threshold:
                print(f"[{task}] exiting after {consecutive_failures} consecutive failures so supervisor health/restart policy can act.", file=sys.stderr, flush=True)
                return 1
        else:
            consecutive_failures = 0
            _record_health(task, ok=True)
        if once:
            return 0 if result.ok else 1
        deadline = time.monotonic() + interval
        while not STOP and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=sorted(TASKS))
    parser.add_argument("--interval-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    return run_loop(args.task, interval_seconds=args.interval_seconds, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
