#!/usr/bin/env python3
"""One unprivileged, offline audit worker for a selected local repo copy.

The worker emits evidence rather than making source changes.  It accepts only a
small allowlist of test commands; no shell, network, credentials, or host home
directory are exposed by the compose profile.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

COMMANDS = {
    "python_compile": ["python", "-m", "compileall", "-q", "."],
    "pytest": ["python", "-m", "pytest", "-q"],
    "npm_test": ["npm", "test", "--", "--runInBand"],
}


def run(name: str, repo: Path, timeout: int) -> dict:
    """Execute exactly one allowlisted check and return bounded diagnostic output."""
    command = COMMANDS[name]
    try:
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=timeout)
        return {"name": name, "command": command, "exit_code": result.returncode,
                "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:]}
    except FileNotFoundError:
        return {"name": name, "command": command, "exit_code": None,
                "error": "required runtime is not installed in this worker image"}
    except subprocess.TimeoutExpired:
        return {"name": name, "command": command, "exit_code": None, "error": f"timed out after {timeout}s"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline JobOS repository audit worker")
    parser.add_argument("--repo", required=True, help="Path below /input mounted read-only")
    parser.add_argument("--check", choices=sorted(COMMANDS), action="append", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", default="/reports/repo_audit_report.json")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    input_root = Path("/input").resolve()
    if input_root not in repo.parents or not (repo / ".git").exists():
        raise SystemExit("--repo must be a git checkout under the read-only /input mount")
    report = {"repo": repo.name, "path": str(repo), "network": "disabled", "checks": [run(name, repo, args.timeout) for name in args.check]}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
