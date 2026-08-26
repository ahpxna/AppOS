#!/usr/bin/env python3
"""High-reasoning report coordinator; it cannot execute code or modify repos."""
from __future__ import annotations

# JOBOS_DIRECT_FILE_BOOTSTRAP: keep direct `python path/to/file.py` usable
# while package imports resolve exactly as they do under `python -m ...`.
import sys as _jobos_sys
from pathlib import Path as _JobOSPath
_JOBOS_ROOT = _JobOSPath(__file__).resolve().parents[2]
if str(_JOBOS_ROOT) not in _jobos_sys.path:
    _jobos_sys.path.insert(0, str(_JOBOS_ROOT))

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from services.common.openclaw_runtime import resolve_openclaw_binary

OPENCLAW_BIN = resolve_openclaw_binary()
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT_REPO_COORDINATOR", "repo_coordinator")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise isolated repo-worker reports")
    parser.add_argument("reports", nargs="+", help="JSON reports emitted by repo_worker_v1.py")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.reports]
    prompt = """You are the JobOS repository-audit coordinator. Analyse ONLY these worker reports.
You cannot run commands, access repositories, edit files, or request secrets.
Return JSON with: summary, failures [{repo, check, likely_cause, evidence}],
recommended_next_tasks [{repo, purpose, allowed_check}], and risks. Do not claim
you inspected source code; workers only supplied reports.\n\nREPORTS:\n""" + json.dumps(reports, indent=2)
    if shutil.which(OPENCLAW_BIN) is None:
        raise SystemExit(f"{OPENCLAW_BIN!r} not found; reports remain available for manual review.")
    result = subprocess.run([OPENCLAW_BIN, "agent", "--agent", OPENCLAW_AGENT, "--message", prompt,
                             "--json", "--timeout", str(args.timeout), "--session-id", "jobos-repo-audit"],
                            capture_output=True, text=True, timeout=args.timeout + 30)
    print(result.stdout if result.returncode == 0 else result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
