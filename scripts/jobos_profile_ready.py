#!/usr/bin/env python3
"""One-command candidate-profile pipeline: raw documents -> resume-ready context.

Operator UX
-----------
Drop candidate documents into ``data/profile_raw`` and run::

    python scripts/jobos_profile_ready.py run

The command is resumable and intentionally stops at the only human-owned gate:
review of generated ``profile_assets``. After reviewing/approving assets, run the
same command again. It then builds deterministic capabilities, profile briefs,
and base context packs and reports whether ``base_resume_generation`` is ready.

Supported drop-folder layout::

    data/profile_raw/
      resume.docx                 # root defaults to official evidence
      official/transcript.pdf
      course/course_notes.docx
      project/applyops.md
      mapping/portfolio_map.docx
      reference/paper.pdf
      guidance/career_notes.docx

This file orchestrates the existing canonical modules. It does not duplicate
mapping/evidence/synthesis logic and it never auto-approves profile claims.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "data" / "profile_raw"
SOURCE_ROOT = ROOT / "data" / "profile_sources_v2"
PARSED_ROOT = ROOT / "data" / "profile_parsed_v2"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
REQUIREMENTS = ROOT / "requirements.txt"
MANIFEST_PATH = SOURCE_ROOT / ".jobos_profile_ready_manifest.json"
TEMPLATE = ROOT / "data" / "resume-template" / "VU PHAN AN NGUYEN-official_For_all.docx"

SUPPORTED_RAW_SUFFIXES = {".docx", ".pdf", ".md", ".txt"}
BUCKETS = {
    "official": "00_official",
    "00_official": "00_official",
    "course": "01_course_profiles",
    "01_course_profiles": "01_course_profiles",
    "project": "02_project_profiles",
    "02_project_profiles": "02_project_profiles",
    "mapping": "03_cross_portfolio_mappings",
    "03_cross_portfolio_mappings": "03_cross_portfolio_mappings",
    "reference": "04_source_papers_and_course_readings",
    "04_source_papers_and_course_readings": "04_source_papers_and_course_readings",
    "guidance": "05_guidance_not_truth",
    "05_guidance_not_truth": "05_guidance_not_truth",
}
REQUIRED_IMPORTS = ("psycopg", "pypdf", "docx", "pglast")
GENERIC_EVIDENCE_VERSION = "profile_evidence_unit_builder_qwen_v2_2026_08_25"
STRUCTURED_EVIDENCE_VERSION = "structured_evidence_unit_builder_qwen_v2_2026_04_27"
GENERIC_ASSET_VERSION = "profile_asset_synthesizer_qwen_v2_2026_08_25"
STRUCTURED_ASSET_VERSION = "structured_tool_workflow_asset_synthesizer_qwen_v1_2026_04_27"


class PipelineError(RuntimeError):
    pass


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def is_real_source(path: Path) -> bool:
    if not path.is_file() or path.name.startswith("."):
        return False
    if ":Zone.Identifier" in path.name or path.name.endswith("Zone.Identifier"):
        return False
    return path.suffix.casefold() in SUPPORTED_RAW_SUFFIXES


def raw_documents() -> list[Path]:
    if not RAW_ROOT.exists():
        return []
    return sorted(p for p in RAW_ROOT.rglob("*") if is_real_source(p))


def bucket_for(path: Path) -> str:
    rel = path.relative_to(RAW_ROOT)
    if len(rel.parts) <= 1:
        return "00_official"
    return BUCKETS.get(rel.parts[0].casefold(), "00_official")


def _load_stage_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read staging manifest {MANIFEST_PATH}: {exc}") from exc
    entries = data.get("entries", {}) if isinstance(data, dict) else {}
    return entries if isinstance(entries, dict) else {}


def _write_stage_manifest(entries: dict[str, dict[str, str]]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "entries": entries}, indent=2), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def stage_raw_documents(*, replace: bool) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    docs = raw_documents()
    if not docs:
        raise PipelineError(
            f"No supported documents found under {RAW_ROOT}. "
            "Add at least one .docx/.pdf/.md/.txt file before running the profile pipeline."
        )

    previous = _load_stage_manifest()
    desired_sources = {str(source.relative_to(RAW_ROOT)) for source in docs}

    # Remove only files that this runner staged previously. Never delete an
    # untracked/canonical source from profile_sources_v2.
    removed: list[dict[str, str]] = []
    for source_rel, entry in previous.items():
        if source_rel in desired_sources:
            continue
        dest_rel = str(entry.get("destination") or "")
        expected_sha = str(entry.get("sha256") or "")
        destination = SOURCE_ROOT / dest_rel if dest_rel else None
        if destination and destination.is_file():
            if expected_sha and sha256_file(destination) != expected_sha:
                raise PipelineError(
                    f"Managed staged source {destination} changed outside the runner; refusing to prune it automatically."
                )
            destination.unlink()
            sidecar = (PARSED_ROOT / dest_rel).with_suffix(".txt")
            if sidecar.is_file():
                sidecar.unlink()
        removed.append({
            "source": source_rel,
            "destination": str((SOURCE_ROOT / dest_rel).relative_to(ROOT)) if dest_rel else "",
            "sha256": expected_sha,
            "action": "removed",
        })

    staged: list[dict[str, str]] = []
    manifest: dict[str, dict[str, str]] = {}
    for source in docs:
        bucket = bucket_for(source)
        destination = SOURCE_ROOT / bucket / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_sha = sha256_file(source)

        action = "copied"
        if destination.exists():
            if sha256_file(destination) == source_sha:
                action = "unchanged"
            elif replace:
                shutil.copy2(source, destination)
                action = "replaced"
            else:
                raise PipelineError(
                    f"Staging conflict: {destination} differs from {source}. "
                    "Re-run with --replace-staged only if replacement is intentional."
                )
        else:
            shutil.copy2(source, destination)

        source_rel = str(source.relative_to(RAW_ROOT))
        dest_rel = str(destination.relative_to(SOURCE_ROOT))
        manifest[source_rel] = {"destination": dest_rel, "sha256": source_sha}
        staged.append({
            "source": str(source.relative_to(ROOT)),
            "destination": str(destination.relative_to(ROOT)),
            "bucket": bucket,
            "sha256": source_sha,
            "action": action,
        })
    _write_stage_manifest(manifest)
    return staged, removed



def deactivate_removed_managed_sources(removed: list[dict[str, str]]) -> int:
    """Retire DB truth derived from drop-folder files intentionally removed by the operator."""
    if not removed:
        return 0
    psycopg, database_dsn = load_db_helpers()
    retired = 0
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        for item in removed:
            destination = str((ROOT / item["destination"]).resolve()) if item.get("destination") else ""
            cur.execute(
                """SELECT id FROM raw_files
                     WHERE source = 'profile_sources_v2' AND sha256 = %s
                       AND original_local_path = %s AND is_active = true
                     FOR UPDATE""",
                (item.get("sha256"), destination),
            )
            row = cur.fetchone()
            if not row:
                continue
            raw_id = row[0]
            cur.execute("UPDATE raw_files SET is_active = false WHERE id = %s", (raw_id,))
            cur.execute(
                """UPDATE profile_documents
                      SET status = 'superseded', contains_profile_evidence = false, updated_at = now()
                    WHERE raw_file_id = %s AND status <> 'superseded'""",
                (raw_id,),
            )
            cur.execute(
                """UPDATE profile_assets
                      SET status = 'superseded', updated_at = now(),
                          review_note = concat_ws(' ', nullif(review_note, ''),
                              'Source removed from data/profile_raw; asset retired by unified profile pipeline.')
                    WHERE status <> 'superseded' AND (
                          created_from_raw_file_id = %s OR id IN (
                              SELECT profile_asset_id FROM profile_asset_evidence_items WHERE raw_file_id = %s
                          )
                    )""",
                (raw_id, raw_id),
            )
            retired += 1
        conn.commit()
    return retired

def write_plain_text_sidecars() -> int:
    """Make .md/.txt sources usable by the V2 ingestor without another parser."""
    created = 0
    if not SOURCE_ROOT.exists():
        return 0
    for source in SOURCE_ROOT.rglob("*"):
        if not source.is_file() or source.suffix.casefold() not in {".md", ".txt"}:
            continue
        rel = source.relative_to(SOURCE_ROOT)
        out = (PARSED_ROOT / rel).with_suffix(".txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8", errors="replace")
        if not out.exists() or out.read_text(encoding="utf-8", errors="replace") != text:
            out.write_text(text, encoding="utf-8")
            created += 1
    return created


def import_probe() -> list[str]:
    missing: list[str] = []
    for module in REQUIRED_IMPORTS:
        if importlib.util.find_spec(module) is None:
            missing.append(module)
    return missing


def ensure_venv_and_reexec(*, no_install: bool) -> None:
    """Bootstrap the project venv once, then re-exec this command inside it."""
    if os.getenv("JOBOS_PROFILE_READY_REEXEC") == "1":
        missing = import_probe()
        if missing:
            raise PipelineError(
                "Missing Python dependencies inside .venv: " + ", ".join(missing) +
                ". Run .venv/bin/python -m pip install -r requirements.txt"
            )
        return

    running_in_repo_venv = Path(sys.executable).resolve() == VENV_PYTHON.resolve() if VENV_PYTHON.exists() else False
    if running_in_repo_venv:
        missing = import_probe()
        if missing:
            if no_install:
                raise PipelineError("Missing Python dependencies: " + ", ".join(missing))
            run_checked([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)], label="install_requirements")
        return

    if not VENV_PYTHON.exists():
        if no_install:
            raise PipelineError(".venv is missing and --no-install was supplied.")
        run_checked([sys.executable, "-m", "venv", str(ROOT / ".venv")], label="create_venv", cwd=ROOT)

    probe = subprocess.run(
        [str(VENV_PYTHON), "-c", "import psycopg,pypdf,docx,pglast"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        if no_install:
            raise PipelineError(".venv exists but required packages are missing and --no-install was supplied.")
        run_checked([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)], label="install_requirements", cwd=ROOT)

    env = os.environ.copy()
    env["JOBOS_PROFILE_READY_REEXEC"] = "1"
    os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], env)


def run_checked(cmd: list[str], *, label: str, cwd: Path = ROOT, allow_codes: Iterable[int] = (0,)) -> subprocess.CompletedProcess[str]:
    emit("stage_start", stage=label, command=" ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, text=True)
    if proc.returncode not in set(allow_codes):
        raise PipelineError(f"Stage {label!r} failed with exit code {proc.returncode}.")
    emit("stage_finish", stage=label, returncode=proc.returncode)
    return proc


def load_db_helpers():
    sys.path.insert(0, str(ROOT))
    from services.common.config import database_dsn, load_repo_env
    load_repo_env()
    import psycopg
    return psycopg, database_dsn


def can_connect_db() -> tuple[bool, str]:
    try:
        psycopg, database_dsn = load_db_helpers()
        with psycopg.connect(database_dsn(), connect_timeout=4) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "ok"
    except Exception as exc:  # bounded health diagnostic only
        return False, str(exc).splitlines()[0][:300]


def ensure_database(*, no_start_db: bool) -> None:
    ok, detail = can_connect_db()
    if ok:
        emit("database", status="ready")
        return
    if no_start_db:
        raise PipelineError(f"PostgreSQL unavailable: {detail}")
    if shutil.which("docker") is None:
        raise PipelineError(
            f"PostgreSQL unavailable ({detail}) and Docker is not installed. "
            "Start the configured PostgreSQL instance manually."
        )
    run_checked(["docker", "compose", "up", "-d", "postgres"], label="start_postgres")
    deadline = time.monotonic() + 45
    last = detail
    while time.monotonic() < deadline:
        ok, last = can_connect_db()
        if ok:
            emit("database", status="ready")
            return
        time.sleep(1.5)
    raise PipelineError(f"PostgreSQL did not become ready: {last}")


def apply_schema() -> None:
    run_checked([sys.executable, "scripts/migration_lint.py"], label="migration_lint")
    run_checked([sys.executable, "scripts/apply_migrations.py"], label="apply_migrations")


def query_profile_state() -> dict[str, Any]:
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
            cur.execute(sql, params)
            return int(cur.fetchone()[0])

        state = {
            "documents_total": scalar("SELECT count(*) FROM profile_documents"),
            "documents_needs_mapping": scalar("SELECT count(*) FROM profile_documents WHERE status = 'needs_mapping'"),
            "documents_ready_for_evidence": scalar("SELECT count(*) FROM v_profile_documents_ready_for_evidence"),
            "candidate_docs_not_ready_for_evidence": scalar(
                """SELECT count(*)
                   FROM profile_documents pd
                   WHERE pd.contains_profile_evidence = true
                     AND pd.status = 'mapped'
                     AND NOT EXISTS (
                       SELECT 1 FROM v_profile_documents_ready_for_evidence q
                       WHERE q.profile_document_id = pd.id
                     )"""
            ),
            "map_blocked_or_review": scalar(
                "SELECT count(*) FROM v_profile_documents_blocked_from_evidence WHERE recommended_action <> 'ignore_for_truth'"
            ),
            "generic_evidence_units": scalar(
                "SELECT count(*) FROM profile_evidence_units WHERE builder_version = %s",
                (GENERIC_EVIDENCE_VERSION,),
            ),
            "structured_evidence_units": scalar(
                "SELECT count(*) FROM profile_evidence_units WHERE builder_version = %s",
                (STRUCTURED_EVIDENCE_VERSION,),
            ),
            "ready_docs_without_generic_evidence": scalar(
                """SELECT count(*)
                   FROM v_profile_documents_ready_for_evidence q
                   JOIN profile_documents pd ON pd.id = q.profile_document_id
                   WHERE pd.contains_profile_evidence = true
                     AND NOT EXISTS (
                       SELECT 1 FROM profile_evidence_units peu
                       WHERE peu.profile_document_id = pd.id AND peu.builder_version = %s
                     )""",
                (GENERIC_EVIDENCE_VERSION,),
            ),
            "ready_docs_without_generic_asset": scalar(
                """SELECT count(*)
                   FROM v_profile_documents_ready_for_evidence q
                   JOIN profile_documents pd ON pd.id = q.profile_document_id
                   JOIN raw_files rf ON rf.id = pd.raw_file_id
                   WHERE pd.contains_profile_evidence = true
                     AND EXISTS (
                       SELECT 1 FROM profile_evidence_units peu
                       WHERE peu.profile_document_id = pd.id AND peu.builder_version = %s
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM profile_assets pa
                       WHERE pa.created_from_raw_file_id = rf.id AND pa.compiler_version = %s
                     )""",
                (GENERIC_EVIDENCE_VERSION, GENERIC_ASSET_VERSION),
            ),
            "assets_draft": scalar("SELECT count(*) FROM profile_assets WHERE status IN ('draft','needs_review','pending_review')"),
            "assets_approved": scalar("SELECT count(*) FROM profile_assets WHERE status = 'approved'"),
            "assets_rejected": scalar("SELECT count(*) FROM profile_assets WHERE status = 'rejected'"),
            "capabilities_approved": scalar("SELECT count(*) FROM profile_capabilities WHERE status = 'approved'"),
            "fresh_briefs": scalar("SELECT count(*) FROM profile_briefs WHERE is_stale = false"),
            "resume_base_pack": scalar(
                """SELECT count(*) FROM profile_context_packs
                   WHERE application_id IS NULL AND message_thread_id IS NULL
                     AND purpose = 'base_resume_generation'"""
            ),
        }
        cur.execute(
            """SELECT id::text, asset_title, asset_type, status, round(confidence::numeric, 2),
                      left(canonical_narrative, 420)
               FROM profile_assets
               WHERE status IN ('draft','needs_review','pending_review')
               ORDER BY updated_at, asset_title LIMIT 50"""
        )
        state["review_queue"] = [
            {"id": r[0], "title": r[1], "type": r[2], "status": r[3], "confidence": str(r[4]), "preview": r[5]}
            for r in cur.fetchall()
        ]
        return state



def staged_sources_synced(staged: list[dict[str, str]]) -> bool:
    """Return True when every drop-folder source has the same staged DB identity."""
    if not staged:
        return False
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        for item in staged:
            source = ROOT / item["destination"]
            digest = str(item["sha256"])
            cur.execute(
                """SELECT EXISTS (
                     SELECT 1
                     FROM raw_files rf
                     JOIN profile_documents pd ON pd.raw_file_id = rf.id
                     WHERE rf.source = 'profile_sources_v2'
                       AND rf.sha256 = %s
                       AND rf.original_local_path = %s
                       AND rf.is_active = true
                   )""",
                (digest, str(source.resolve())),
            )
            if not bool(cur.fetchone()[0]):
                return False
    return True

def run_sql_builder(path: Path, *, label: str) -> None:
    psycopg, database_dsn = load_db_helpers()
    sql = path.read_text(encoding="utf-8")
    lines = sql.splitlines()
    meaningful = [i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("--")]
    if meaningful and lines[meaningful[0]].strip().upper() == "BEGIN;" and lines[meaningful[-1]].strip().upper() == "COMMIT;":
        del lines[meaningful[-1]]
        del lines[meaningful[0]]
        sql = "\n".join(lines)
    emit("stage_start", stage=label, file=str(path.relative_to(ROOT)))
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    emit("stage_finish", stage=label, returncode=0)


def list_review_assets() -> int:
    state = query_profile_state()
    print(json.dumps({"review_required": state["assets_draft"] > 0, "assets": state["review_queue"]}, indent=2))
    return 0


def approve_asset(asset_id: str, note: str, *, apply: bool) -> int:
    cmd = [sys.executable, "scripts/jobos_profile_onboarding.py", "approve", asset_id, "--note", note]
    if apply:
        cmd.append("--apply")
    run_checked(cmd, label="approve_profile_asset")
    return 0


def reject_asset(asset_id: str, note: str, *, apply: bool) -> int:
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT asset_title, status FROM profile_assets WHERE id = %s FOR UPDATE",
            (asset_id,),
        )
        row = cur.fetchone()
        if not row:
            raise PipelineError("Profile asset not found.")
        if row[1] not in {"draft", "needs_review", "pending_review"}:
            raise PipelineError(f"Only review-pending assets can be rejected (current: {row[1]}).")
        payload = {"id": asset_id, "title": row[0], "previous_status": row[1], "apply": apply}
        if apply:
            cur.execute(
                "UPDATE profile_assets SET status='rejected', review_note=%s, updated_at=now() WHERE id=%s",
                (note.strip() or "Rejected manually through unified profile pipeline.", asset_id),
            )
            conn.commit()
            payload["status"] = "rejected"
        else:
            conn.rollback()
            payload["status"] = "dry_run"
    print(json.dumps(payload, indent=2))
    return 0


def sync_source_revisions() -> None:
    run_checked([
        sys.executable, "services/profile-ingestion/profile_source_revisions_v1.py",
        "sync", "--apply",
    ], label="sync_profile_source_revisions")


def fixed_fields_status() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from services.common.profile_freshness import assess_resume_profile
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        return assess_resume_profile(cur)


def run_fixed_fields_wizard() -> None:
    run_checked([
        sys.executable, "services/profile-ingestion/fixed_profile_fields_v1.py",
        "wizard", "--actor", "candidate", "--apply",
    ], label="fixed_resume_fields_wizard")


def run_github_refresh(*, skip: bool = False) -> None:
    if skip:
        emit("stage_skip", stage="github_project_refresh", reason="explicit --skip-github-refresh")
        return
    run_checked([
        sys.executable, "services/repo-audit/repository_freshness_v1.py",
        "refresh",
    ], label="github_project_refresh")


def freshness_payload() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from services.common.profile_freshness import assess_resume_profile, explain_blockers
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        report = assess_resume_profile(cur)
    return {"resume": report, "blockers": explain_blockers(report)}


def configure_project_source(project_id: str, repo_full_name: str, branch: str, *, document_only: bool = False) -> int:
    sys.path.insert(0, str(ROOT))
    from services.common.project_registry import load_registry, save_registry, ProjectRegistryError
    registry = load_registry()
    matched = False
    for project in registry["projects"]:
        if project["project_id"] != project_id:
            continue
        matched = True
        if document_only:
            project["dynamic_source_mode"] = "document_only"
            project["github_repo_full_name"] = ""
        else:
            if "/" not in repo_full_name:
                raise PipelineError("--repo must be owner/repository.")
            project["dynamic_source_mode"] = "github_primary"
            project["github_repo_full_name"] = repo_full_name.strip()
            project["github_default_branch"] = branch.strip() or "main"
        break
    if not matched:
        raise PipelineError(f"Unknown fixed project_id: {project_id}")
    try:
        path = save_registry(registry)
    except ProjectRegistryError as exc:
        raise PipelineError(str(exc)) from exc
    print(json.dumps({"project_id": project_id, "registry": str(path), "document_only": document_only,
                      "repo": None if document_only else repo_full_name, "branch": None if document_only else branch}, indent=2))
    return 0


def confirm_repository(repo_full_name: str, actor: str, *, apply: bool) -> int:
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, ownership_status FROM repository_evidence_sources WHERE provider='github' AND repo_full_name=%s FOR UPDATE",
            (repo_full_name,),
        )
        row = cur.fetchone()
        if not row:
            raise PipelineError("Repository source not imported yet. Run `refresh` once, then confirm ownership.")
        cur.execute(
            """
            UPDATE repository_evidence_sources
            SET ownership_status='confirmed_by_user', status='ownership_confirmed',
                ownership_confirmed_at=now(), ownership_confirmed_by=%s, updated_at=now()
            WHERE id=%s;
            """,
            (actor, row[0]),
        )
        result = {"repository_source_id": row[0], "repo": repo_full_name,
                  "ownership_status": "confirmed_by_user", "committed": apply}
        if apply:
            conn.commit()
        else:
            conn.rollback()
    print(json.dumps(result, indent=2))
    return 0


def run_to_review(*, replace_staged: bool, ingest_sources: bool, force_parse: bool) -> dict[str, Any]:
    if ingest_sources:
        sidecars = write_plain_text_sidecars()
        emit("plain_text_sidecars", written=sidecars)

        existing_bridge = ROOT / "build_profile_parsed_v2_from_existing.py"
        if existing_bridge.exists():
            run_checked([sys.executable, str(existing_bridge)], label="reuse_existing_parsed_text")

        parse_cmd = [sys.executable, "services/profile-ingestion/parse_profile_sources_v2.py"]
        if force_parse:
            parse_cmd.append("--force")
        run_checked(parse_cmd, label="parse_profile_sources")
        # Handle raw .md/.txt after parser reports them as unsupported; the ingestor
        # sees the sidecars created above.
        write_plain_text_sidecars()
        run_checked([sys.executable, "services/profile-ingestion/ingest_profile_sources_v2.py", "--apply"], label="ingest_profile_sources")
    else:
        emit("stage_skip", stage="parse_and_ingest", reason="staged source SHA identities already exist in DB")

    # Always refresh logical-source/revision provenance. Content SHA decides
    # revision identity; Office/PDF timestamps are retained only as metadata.
    sync_source_revisions()

    run_checked([
        sys.executable, "services/profile-ingestion/map_profile_documents_qwen_v1.py",
        "--apply", "--limit", "1000",
    ], label="map_profile_documents")
    run_checked([
        sys.executable, "services/profile-ingestion/audit_profile_document_maps.py",
        "--apply", "--limit", "2000",
    ], label="audit_profile_document_maps")

    # Optional enrichment. This selects only structured inventory/mapping/tool
    # documents and is a no-op for a normal resume/transcript-only profile.
    run_checked([
        sys.executable, "services/profile-ingestion/build_structured_sections_from_inventory_docs.py",
        "--apply", "--limit", "1000",
    ], label="build_structured_sections")

    state = query_profile_state()
    if state["documents_needs_mapping"]:
        raise PipelineError(
            f"{state['documents_needs_mapping']} document(s) remain unmapped. "
            "Inspect model/gateway errors before evidence extraction."
        )
    if state["map_blocked_or_review"]:
        raise PipelineError(
            f"{state['map_blocked_or_review']} mapped document(s) are blocked by the deterministic map audit. "
            "Review/remap them before continuing."
        )
    if state["documents_ready_for_evidence"] == 0:
        raise PipelineError("No candidate-truth documents passed the map quality gate.")

    # Generic branch: required for normal resume/transcript/project documents.
    # The generic asset synthesizer is intentionally wired to the V1 evidence
    # builder_version, so removing this branch would yield zero assets for a
    # normal DOCX even though the structured V2 builder ran successfully.
    run_checked([
        sys.executable, "services/profile-ingestion/build_profile_evidence_units_qwen_v1.py",
        "--apply", "--limit", "1000",
    ], label="build_generic_profile_evidence")

    # Structured V2 is an enrichment branch for mapping/tool-inventory sections,
    # not a replacement for generic evidence extraction.
    run_checked([
        sys.executable, "services/profile-ingestion/build_structured_evidence_units_qwen_v2.py",
        "--apply", "--limit", "5000",
    ], label="build_structured_evidence")

    state = query_profile_state()
    if state["ready_docs_without_generic_evidence"]:
        raise PipelineError(
            f"{state['ready_docs_without_generic_evidence']} candidate-truth document(s) produced no generic evidence units. "
            "Inspect the evidence-builder/model output before synthesizing resume claims."
        )

    run_checked([
        sys.executable, "services/profile-ingestion/synthesize_profile_assets_qwen_v1.py",
        "--apply", "--limit", "1000", "--max-assets-per-doc", "2",
    ], label="synthesize_generic_profile_assets")

    # If structured V2 evidence exists, synthesize its richer workflow assets as
    # additional review candidates. Empty --file-like means all V2 evidence; the
    # V2 builder itself already limits input to structured inventory sections.
    state = query_profile_state()
    if state["structured_evidence_units"]:
        run_checked([
            sys.executable, "services/profile-ingestion/synthesize_structured_tool_workflow_assets_qwen_v1.py",
            "--apply", "--file-like", "", "--limit-workflows", "1000", "--limit-units", "5000",
        ], label="synthesize_structured_workflow_assets")

    state = query_profile_state()
    if state["ready_docs_without_generic_asset"]:
        raise PipelineError(
            f"{state['ready_docs_without_generic_asset']} candidate-truth document(s) have evidence but no synthesized generic asset. "
            "Inspect asset synthesis before review."
        )
    return state


def finish_after_review() -> dict[str, Any]:
    state = query_profile_state()
    if state["assets_draft"]:
        return state
    if state["assets_approved"] == 0:
        raise PipelineError("No approved profile assets exist. Review and approve at least one grounded asset first.")

    run_sql_builder(
        ROOT / "services/profile-ingestion/rebuild_profile_capabilities_v2.sql",
        label="rebuild_profile_capabilities",
    )
    run_checked([
        sys.executable, "services/profile-ingestion/prepare_profile_for_pipeline_v1.py",
        "build", "--apply",
    ], label="build_profile_briefs_and_context_packs")
    return query_profile_state()


def run_pipeline(args: argparse.Namespace) -> int:
    ensure_database(no_start_db=args.no_start_db)
    apply_schema()

    # Stage first so the sync check reflects exactly what the operator dropped
    # into data/profile_raw. This makes the command genuinely resumable: the
    # second run after human review does not destructively re-ingest unchanged
    # sources and does not repeat LLM stages.
    staged, removed = stage_raw_documents(replace=args.replace_staged)
    retired = deactivate_removed_managed_sources(removed)
    emit("raw_sources", count=len(staged), files=staged, removed=removed, retired_sources=retired)
    synced = staged_sources_synced(staged)

    before = query_profile_state()
    emit("profile_state_before", sources_synced=synced, **{k: v for k, v in before.items() if k != "review_queue"})

    processing_incomplete = bool(
        before["documents_needs_mapping"]
        or before["candidate_docs_not_ready_for_evidence"]
        or before["ready_docs_without_generic_evidence"]
        or before["ready_docs_without_generic_asset"]
        or (before["documents_total"] and before["documents_ready_for_evidence"] == 0 and before["assets_approved"] == 0)
    )

    if synced and before["assets_draft"] and not processing_incomplete:
        state = before
    elif synced and before["assets_approved"] and not before["assets_draft"] and not processing_incomplete:
        state = before
    else:
        state = run_to_review(
            replace_staged=args.replace_staged,
            ingest_sources=not synced,
            force_parse=any(item["action"] != "unchanged" for item in staged),
        )

    # GitHub refresh runs after document-derived candidates exist so the
    # authority-reconciled project asset can combine user-authored purpose with
    # current implementation evidence. Unconfigured projects remain explicit
    # blockers rather than being guessed from old documents.
    run_github_refresh(skip=args.skip_github_refresh)
    state = query_profile_state()

    if state["assets_draft"]:
        print("\nPROFILE ASSET REVIEW REQUIRED")
        print("The automated pipeline stops here by design. Review each claim before it can enter a resume.\n")
        print(json.dumps({"assets": state["review_queue"]}, indent=2))
        print("\nCommands:")
        print("  python scripts/jobos_profile_ready.py review")
        print("  python scripts/jobos_profile_ready.py approve <asset-id> --note 'checked against source' --apply")
        print("  python scripts/jobos_profile_ready.py reject  <asset-id> --note 'not safe/accurate' --apply")
        print("  python scripts/jobos_profile_ready.py run   # resume automatically after review")
        return 3

    fresh = freshness_payload()
    fixed = fresh["resume"]["fixed_fields"]
    if not fixed["fixed_fields_ready"]:
        if not args.non_interactive_fixed_fields and sys.stdin.isatty():
            run_fixed_fields_wizard()
            fresh = freshness_payload()
            fixed = fresh["resume"]["fixed_fields"]
        if not fixed["fixed_fields_ready"]:
            print(json.dumps({
                "resume_profile_ready": False,
                "fixed_fields_ready": False,
                "blockers": fresh["blockers"],
                "next": "Run `python scripts/jobos_profile_ready.py fixed-fields wizard --apply` and confirm the fixed resume fields.",
            }, indent=2))
            return 4

    state = finish_after_review()
    fresh = freshness_payload()
    resume_ready = bool(
        state["resume_base_pack"] and state["assets_approved"] and state["capabilities_approved"]
        and fresh["resume"]["resume_profile_ready"]
    )
    result = {
        "resume_profile_ready": resume_ready,
        "freshness": fresh,
        "resume_template_present": TEMPLATE.is_file(),
        "state": {k: v for k, v in state.items() if k != "review_queue"},
        "next": (
            "Profile context is ready for job-fit/document generation."
            if resume_ready else
            "Profile preparation did not produce the required base resume context pack."
        ),
    }
    print(json.dumps(result, indent=2))
    if not TEMPLATE.is_file():
        print(
            f"WARNING: profile context is ready, but the fixed resume DOCX template is absent at {TEMPLATE}. "
            "Stage it before rendering a resume.",
            file=sys.stderr,
        )
    return 0 if resume_ready else 2


def status_command() -> int:
    docs = raw_documents()
    payload: dict[str, Any] = {
        "raw_root": str(RAW_ROOT),
        "raw_documents": [str(p.relative_to(ROOT)) for p in docs],
        "raw_document_count": len(docs),
        "resume_template_present": TEMPLATE.is_file(),
    }
    ok, detail = can_connect_db()
    payload["database"] = {"ready": ok, "detail": detail}
    if ok:
        payload["profile_state"] = query_profile_state()
        try:
            payload["freshness"] = freshness_payload()
        except Exception as exc:
            payload["freshness"] = {"ready": False, "error": str(exc)}
    print(json.dumps(payload, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-command JobOS profile pipeline from data/profile_raw to resume-ready context."
    )
    parser.add_argument(
        "--no-install", action="store_true",
        help="Do not create/install .venv dependencies; fail if they are missing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run/resume the complete profile pipeline.")
    run.add_argument("--replace-staged", action="store_true", help="Replace changed staged copies intentionally.")
    run.add_argument("--no-start-db", action="store_true", help="Do not start docker-compose PostgreSQL automatically.")
    run.add_argument("--skip-github-refresh", action="store_true", help="Offline diagnostic only: do not poll configured GitHub projects.")
    run.add_argument("--non-interactive-fixed-fields", action="store_true", help="Do not open the fixed-field wizard; report missing fields and stop.")

    sub.add_parser("status", help="Show raw-input and profile readiness state.")
    sub.add_parser("freshness", help="Show fixed-field/document/GitHub freshness and resume blockers.")
    refresh = sub.add_parser("refresh", help="Refresh configured GitHub project snapshots and affected claims.")
    refresh.add_argument("--project-id")
    refresh.add_argument("--watch", action="store_true")
    refresh.add_argument("--interval-seconds", type=int, default=86400)

    fixed = sub.add_parser("fixed-fields", help="Manage user-verified GPA, education, contact and certification fields.")
    fixed_sub = fixed.add_subparsers(dest="fixed_command", required=True)
    fixed_sub.add_parser("status")
    fixed_wiz = fixed_sub.add_parser("wizard")
    fixed_wiz.add_argument("--apply", action="store_true")
    fixed_wiz.add_argument("--actor", default="candidate")

    project_source = sub.add_parser("project-source", help="Bind one fixed resume project to its authoritative GitHub repository.")
    project_source.add_argument("project_id")
    project_source.add_argument("--repo", default="")
    project_source.add_argument("--branch", default="main")
    project_source.add_argument("--document-only", action="store_true")

    confirm_repo = sub.add_parser("confirm-repo", help="Explicitly confirm ownership of one imported repository source.")
    confirm_repo.add_argument("repo_full_name")
    confirm_repo.add_argument("--actor", default="candidate")
    confirm_repo.add_argument("--apply", action="store_true")
    sub.add_parser("review", help="List profile assets waiting for human review.")

    approve = sub.add_parser("approve", help="Approve exactly one reviewed profile asset.")
    approve.add_argument("asset_id")
    approve.add_argument("--note", default="")
    approve.add_argument("--apply", action="store_true")

    reject = sub.add_parser("reject", help="Reject exactly one reviewed profile asset.")
    reject.add_argument("asset_id")
    reject.add_argument("--note", default="")
    reject.add_argument("--apply", action="store_true")

    args = parser.parse_args()

    # Fail immediately on an empty drop folder before venv/DB setup. This is
    # the common operator mistake and should not trigger installs or services.
    if args.command == "run" and not raw_documents():
        print(json.dumps({
            "error": f"No supported documents found under {RAW_ROOT}",
            "resume_profile_ready": False,
        }, indent=2), file=sys.stderr)
        return 1

    # Status/project-source can operate without importing DB dependencies.
    if args.command not in {"status", "project-source"}:
        ensure_venv_and_reexec(no_install=args.no_install)

    try:
        if args.command == "status":
            return status_command()
        if args.command == "freshness":
            print(json.dumps(freshness_payload(), indent=2, default=str))
            return 0
        if args.command == "refresh":
            cmd = [sys.executable, "services/repo-audit/repository_freshness_v1.py"]
            if args.watch:
                cmd += ["watch", "--interval-seconds", str(args.interval_seconds)]
                if args.project_id:
                    cmd += ["--project-id", args.project_id]
            else:
                cmd += ["refresh"]
                if args.project_id:
                    cmd += ["--project-id", args.project_id]
            return subprocess.run(cmd, cwd=ROOT).returncode
        if args.command == "fixed-fields":
            cmd = [sys.executable, "services/profile-ingestion/fixed_profile_fields_v1.py", args.fixed_command]
            if args.fixed_command == "wizard":
                cmd += ["--actor", args.actor]
                if args.apply:
                    cmd.append("--apply")
            return subprocess.run(cmd, cwd=ROOT).returncode
        if args.command == "project-source":
            return configure_project_source(args.project_id, args.repo, args.branch, document_only=args.document_only)
        if args.command == "confirm-repo":
            return confirm_repository(args.repo_full_name, args.actor, apply=args.apply)
        if args.command == "review":
            return list_review_assets()
        if args.command == "approve":
            return approve_asset(args.asset_id, args.note, apply=args.apply)
        if args.command == "reject":
            return reject_asset(args.asset_id, args.note, apply=args.apply)
        return run_pipeline(args)
    except (PipelineError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "resume_profile_ready": False}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
