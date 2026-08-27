from __future__ import annotations

import ast
import json
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


def test_retired_entrypoints_are_preserved_verbatim_in_legacy():
    moved = {
        "apply_patches.py": "legacy/maintenance/apply_patches.py",
        "organize_profile_sources_v2.py": "legacy/maintenance/organize_profile_sources_v2.py",
        "services/profile-ingestion/embed_candidate_facts.py": "legacy/profile-ingestion/embed_candidate_facts.py",
        "services/profile-ingestion/extract_candidate_facts_local.py": "legacy/profile-ingestion/extract_candidate_facts_local.py",
        "services/profile-ingestion/review_candidate_facts.py": "legacy/profile-ingestion/review_candidate_facts.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/bulk_triage_candidate_facts.py": "legacy/profile-ingestion/deprecated_atom_fact_pipeline/bulk_triage_candidate_facts.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/semantic_dedup_candidate_facts.py": "legacy/profile-ingestion/deprecated_atom_fact_pipeline/semantic_dedup_candidate_facts.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/semantic_dedup_candidates.py": "legacy/profile-ingestion/deprecated_atom_fact_pipeline/semantic_dedup_candidates.py",
        "services/profile-ingestion/deprecated_atom_fact_pipeline/verify_candidate_facts.py": "legacy/profile-ingestion/deprecated_atom_fact_pipeline/verify_candidate_facts.py",
    }
    for original, archived in moved.items():
        assert not (ROOT / original).exists(), original
        archived_path = ROOT / archived
        # These are complete historical tools, not empty marker files.  Their
        # original working-tree paths are deliberately absent, so compare the
        # retained archive itself rather than Git history that may predate the
        # retirement move.
        assert archived_path.is_file() and archived_path.stat().st_size > 1024, archived


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


def test_release_container_references_are_digest_pinned():
    paths = [*ROOT.glob("docker-compose*.yml"), ROOT / "Dockerfile.market-intelligence", ROOT / "services/repo-audit/Dockerfile"]
    offenders: list[str] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("image:", "FROM ")) and "@sha256:" not in stripped:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}:{stripped}")
    assert offenders == [], f"release container references must be digest-pinned: {offenders}"


def test_release_policy_declares_constrained_installs_and_digest_pinning():
    policy = json.loads((ROOT / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    assert policy["manifest_kind"] == "release_policy"
    assert policy["python"]["constraints"] == "constraints-v1.txt"
    assert "-c constraints-v1.txt" in policy["python"]["install_command"]
    assert policy["container_policy"]["require_digest_pinning"] is True


def test_profile_bootstrap_uses_the_v1_constraint_set():
    bootstrap = (ROOT / "scripts" / "bootstrap_ubuntu_24.sh").read_text(encoding="utf-8")
    profile_ready = (ROOT / "scripts" / "jobos_profile_ready.py").read_text(encoding="utf-8")
    assert "-c constraints-v1.txt" in bootstrap
    assert 'CONSTRAINTS = ROOT / "constraints-v1.txt"' in profile_ready
    assert '"-c", str(CONSTRAINTS)' in profile_ready


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


def test_release_identity_is_v0_1_0_and_cli_uses_the_policy_profile():
    policy = json.loads((ROOT / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    assert policy["release"]["line"] == "V0.1"
    assert policy["release"]["version"] == "0.1.0"
    assert any("jobos-v0.1.0-source.zip" in gate for gate in policy["release_gates"])

    jobos = (ROOT / "scripts" / "jobos.py").read_text(encoding="utf-8")
    assert 'RELEASE_PROFILE = "v0.1.0"' in jobos
    assert 'choices=(RELEASE_PROFILE,)' in jobos
    assert 'default=RELEASE_PROFILE' in jobos
    assert 'V0.1.0 RELEASE VERIFICATION: PASS' in jobos

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "jobos-v0.1.0-source.zip" in ci
    assert "jobos-v1-source.zip" not in ci
