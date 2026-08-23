"""Resolve the one OpenClaw runtime used by JobOS services."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_OPENCLAW_BIN = REPO_ROOT / "data" / "openclaw-runtime" / "node" / "node_modules" / ".bin" / "openclaw"


def resolve_openclaw_binary(*, required: bool = False) -> str:
    """Prefer explicit non-empty override, managed runtime, then PATH.

    An empty ``OPENCLAW_BIN=`` intentionally means "use JobOS managed
    runtime"; it must not become an empty executable path.
    """
    explicit = os.getenv("OPENCLAW_BIN", "").strip()
    if explicit:
        return explicit
    if PRIVATE_OPENCLAW_BIN.is_file():
        return str(PRIVATE_OPENCLAW_BIN)
    path_binary = shutil.which("openclaw")
    if path_binary:
        return path_binary
    if required:
        raise RuntimeError(
            "OpenClaw runtime is unavailable. Run "
            "python scripts/setup_openclaw_jobos.py --mode native --install-runtime --force "
            "--generate-gateway-token, or set a non-empty OPENCLAW_BIN."
        )
    return "openclaw"
