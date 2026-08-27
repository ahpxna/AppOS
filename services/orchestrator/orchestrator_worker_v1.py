"""Continuous JobOS mechanics worker.

Runs only safe/automatic control-plane work. Human-gated states remain gated by
pipeline metadata and the Human Review Hub. This worker exists so daily users
do not have to run `filter --all` / `advance --all` manually.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = "services.orchestrator.orchestrator_v1"


def _run(*args: str) -> int:
    result = DEFAULT_PROCESS_RUNNER.run(
        [sys.executable, "-m", ORCHESTRATOR, *args], cwd=ROOT, timeout_s=1800,
    )
    if not result.ok:
        detail = (result.output + (f"\n{result.start_error}" if result.start_error else "")).strip()
        if detail:
            print(detail[-2000:], file=sys.stderr)
    return int(result.returncode if result.returncode is not None else 1)


def cycle() -> bool:
    """Run one bounded automatic cycle. Returns True when both phases succeeded."""
    filter_rc = _run("filter", "--all", "--apply")
    advance_rc = _run("advance", "--all", "--apply")
    return filter_rc == 0 and advance_rc == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous safe JobOS orchestrator worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()
    while True:
        ok = cycle()
        if args.once:
            return 0 if ok else 1
        time.sleep(max(3, min(args.poll_seconds, 300)))


if __name__ == "__main__":
    raise SystemExit(main())
