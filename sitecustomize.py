"""Load JobOS's local .env for commands run from its configured virtualenv.

Python imports this module automatically after the bootstrap-created .pth file
adds the repository root to ``sys.path``.  It intentionally implements only
plain ``KEY=VALUE`` parsing: an environment file is configuration, never shell
code to execute.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load()
