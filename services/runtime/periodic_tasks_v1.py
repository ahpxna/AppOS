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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER

STOP = False

TASKS: dict[str, tuple[list[str], int]] = {
    "ats-discovery": ([sys.executable, "-m", "services.discovery.ats_discovery_v1", "poll", "--apply"], 900),
    "profile-discovery": ([sys.executable, "-m", "services.discovery.autonomous_discovery_v1", "run", "--apply"], 120),
    "repo-freshness": ([sys.executable, "services/repo-audit/repository_freshness_v1.py", "refresh"], 900),
}


def run_loop(task: str, *, interval_seconds: int, once: bool = False) -> int:
    global STOP
    if task not in TASKS:
        raise ValueError(f"unknown periodic task: {task}")
    argv, timeout_s = TASKS[task]
    interval = max(60, int(interval_seconds))
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("STOP", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("STOP", True))
    while not STOP:
        result = DEFAULT_PROCESS_RUNNER.run(argv, cwd=ROOT, timeout_s=timeout_s)
        if not result.ok:
            detail = (result.output or result.start_error or "unknown periodic task failure").strip()[-2000:]
            print(f"[{task}] iteration failed (transient={result.transient}): {detail}", file=sys.stderr, flush=True)
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
