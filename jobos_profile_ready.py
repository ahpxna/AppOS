#!/usr/bin/env python3
"""Compatibility shim; canonical implementation lives in scripts/jobos_profile_ready.py."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "scripts" / "jobos_profile_ready.py"), run_name="__main__")
