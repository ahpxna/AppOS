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


def stage_raw_documents(*, replace: bool) -> list[dict[str, str]]:
    docs = raw_documents()
    if not docs:
        raise PipelineError(
            f"No supported documents found under {RAW_ROOT}. "
            "Add at least one .docx/.pdf/.md/.txt file before running the profile pipeline."
        )

    staged: list[dict[str, str]] = []
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

        staged.append({
            "source": str(source.relative_to(ROOT)),
            "destination": str(destination.relative_to(ROOT)),
            "bucket": bucket,
            "sha256": source_sha,
            "action": action,
        })
    return staged


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



def staged_sources_synced() -> bool:
    """Return True when every currently staged source already has the same DB raw-file/document identity."""
    sources = sorted(p for p in SOURCE_ROOT.rglob("*") if is_real_source(p)) if SOURCE_ROOT.exists() else []
    if not sources:
        return False
    psycopg, database_dsn = load_db_helpers()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        for source in sources:
            digest = sha256_file(source)
            cur.execute(
                """SELECT EXISTS (
                     SELECT 1
                     FROM raw_files rf
                     JOIN profile_documents pd ON pd.raw_file_id = rf.id
                     WHERE rf.source = 'profile_sources_v2'
                       AND rf.sha256 = %s
                       AND rf.is_active = true
                   )""",
                (digest,),
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


def run_to_review(*, replace_staged: bool, ingest_sources: bool) -> dict[str, Any]:
    if ingest_sources:
        sidecars = write_plain_text_sidecars()
        emit("plain_text_sidecars", written=sidecars)

        existing_bridge = ROOT / "build_profile_parsed_v2_from_existing.py"
        if existing_bridge.exists():
            run_checked([sys.executable, str(existing_bridge)], label="reuse_existing_parsed_text")

        run_checked([sys.executable, "services/profile-ingestion/parse_profile_sources_v2.py"], label="parse_profile_sources")
        # Handle raw .md/.txt after parser reports them as unsupported; the ingestor
        # sees the sidecars created above.
        write_plain_text_sidecars()
        run_checked([sys.executable, "services/profile-ingestion/ingest_profile_sources_v2.py", "--apply"], label="ingest_profile_sources")
    else:
        emit("stage_skip", stage="parse_and_ingest", reason="staged source SHA identities already exist in DB")

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
    staged = stage_raw_documents(replace=args.replace_staged)
    emit("raw_sources", count=len(staged), files=staged)
    synced = staged_sources_synced()

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
        )

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

    state = finish_after_review()
    resume_ready = bool(state["resume_base_pack"] and state["assets_approved"] and state["capabilities_approved"])
    result = {
        "resume_profile_ready": resume_ready,
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
    print(json.dumps(payload, indent=2))
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

    sub.add_parser("status", help="Show raw-input and profile readiness state.")
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

    # Status can inspect the drop folder without forcing dependency installation.
    if args.command != "status":
        ensure_venv_and_reexec(no_install=args.no_install)

    try:
        if args.command == "status":
            return status_command()
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
