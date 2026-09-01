#!/usr/bin/env python3
"""Run JobOS's private OpenClaw runtime with its compatible Node version.

The project runtime is deliberately separate from a Homebrew/global OpenClaw
installation.  This avoids an incompatible system Node or a package upgrade
changing the browser executor unexpectedly.  It never prints the gateway token
and it does not invoke an agent/model merely by starting the gateway.

Examples:
  python scripts/start_openclaw_jobos.py gateway
  python scripts/start_openclaw_jobos.py health
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "data" / "openclaw-runtime"
OPENCLAW_BIN = RUNTIME_ROOT / "node" / "node_modules" / ".bin" / "openclaw"


def compatible_node_dir() -> Path:
    """Return the project-managed Node runtime, with a clear recovery error."""
    candidates = sorted(RUNTIME_ROOT.glob("node-runtime-*/bin"), reverse=True)
    if not candidates or not (candidates[0] / "node").is_file():
        raise SystemExit(
            "JobOS Node runtime is missing. Re-run the OpenClaw runtime setup "
            "or install a supported Node 24 release before starting the gateway."
        )
    return candidates[0]


def runtime_env() -> dict[str, str]:
    """Keep the private Node/OpenClaw pair ahead of any global installation."""
    if not OPENCLAW_BIN.is_file():
        raise SystemExit(
            "Private OpenClaw is missing. Install it under "
            "data/openclaw-runtime/node before starting JobOS."
        )
    env = dict(os.environ)
    env["PATH"] = f"{compatible_node_dir()}:{env.get('PATH', '')}"
    env["OPENCLAW_BIN"] = str(OPENCLAW_BIN)
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Start or health-check JobOS's private OpenClaw runtime.")
    parser.add_argument("action", choices=("gateway", "health"), nargs="?", default="gateway")
    args = parser.parse_args()
    env = runtime_env()
    command = [str(OPENCLAW_BIN), "gateway", "run", "--compact"]
    if args.action == "health":
        # OpenClaw 2026.7 may return process status 0 even when its own
        # connectivity probe says `failed`. Translate the semantic health
        # result into the CLI contract expected by operators and automation.
        completed = subprocess.run(
            [str(OPENCLAW_BIN), "gateway", "status"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = completed.stdout or ""
        print(output, end="" if output.endswith("\n") else "\n")
        if completed.returncode != 0:
            return completed.returncode
        normalized = output.lower()
        if "connectivity probe: ok" not in normalized:
            return 1
        return 0
    os.execvpe(command[0], command, env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
