from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _production_python_files():
    for base in (ROOT / "services", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if "legacy" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def test_no_import_time_database_dsn_binding():
    offenders: list[str] = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            value = None
            if isinstance(node, ast.Assign):
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                value = node.value
            if not isinstance(value, ast.Call):
                continue
            if isinstance(value.func, ast.Name) and value.func.id == "database_dsn":
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"database_dsn() must be resolved at runtime, not import time: {offenders}"


def test_retired_legacy_entrypoints_are_not_silent_active_files():
    retired = [
        "apply_patches.py",
        "organize_profile_sources_v2.py",
        "services/profile-ingestion/embed_candidate_facts.py",
        "services/profile-ingestion/extract_candidate_facts_local.py",
        "services/profile-ingestion/review_candidate_facts.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/bulk_triage_candidate_facts.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/semantic_dedup_candidate_facts.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/semantic_dedup_candidates.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/verify_candidate_facts.py",
    ]
    present = [rel for rel in retired if (ROOT / rel).exists()]
    assert present == [], f"retired entrypoints must live only under legacy/: {present}"


def test_release_compose_files_do_not_use_floating_latest_tags():
    offenders: list[str] = []
    for path in ROOT.glob("docker-compose*.yml"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ":latest" in stripped:
                offenders.append(f"{path.name}:{lineno}:{stripped}")
    assert offenders == [], f"release compose images must be pinned: {offenders}"


def test_v1_constraints_cover_direct_python_runtime_dependencies():
    constraints = (ROOT / "constraints-v1.txt").read_text(encoding="utf-8").lower()
    required = {
        "psycopg",
        "pglast",
        "pytest",
        "pynput",
        "cryptography",
        "python-docx",
        "pypdf",
        "requests",
        "websocket-client",
        "reportlab",
    }
    missing = sorted(name for name in required if name not in constraints)
    assert missing == [], f"missing V1 release constraints: {missing}"
