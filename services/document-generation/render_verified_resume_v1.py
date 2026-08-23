#!/usr/bin/env python3
"""Write the latest QA-passed tailoring into a local fixed-layout DOCX."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

import importlib.util
import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RENDERER_PATH = ROOT / "services" / "document-generation" / "resume_template_renderer.py"
RENDERER_SPEC = importlib.util.spec_from_file_location("jobos_resume_template_renderer", RENDERER_PATH)
if RENDERER_SPEC is None or RENDERER_SPEC.loader is None:
    raise RuntimeError(f"Cannot load resume renderer: {RENDERER_PATH}")
renderer = importlib.util.module_from_spec(RENDERER_SPEC)
RENDERER_SPEC.loader.exec_module(renderer)
render_docx = renderer.render_docx
ResumeTemplateError = renderer.ResumeTemplateError

DSN = " ".join((
    f"host={os.getenv('JOBOS_DB_HOST', '127.0.0.1')}", f"port={os.getenv('JOBOS_DB_PORT', '5433')}",
    f"dbname={os.getenv('JOBOS_DB_NAME', 'job_apply_os')}", f"user={os.getenv('JOBOS_DB_USER', 'jobos')}",
    f"password={os.getenv('JOBOS_DB_PASSWORD', 'jobos_local_dev_password_change_later')}",
))
TEMPLATE = Path(os.getenv("JOBOS_RESUME_TEMPLATE_PATH", ROOT / "data/resume-template/VU PHAN AN NGUYEN-official_For_all.docx"))
OUTPUT_ROOT = Path(os.getenv("JOBOS_RESUME_OUTPUT_DIR", ROOT / "data/generated-resumes"))


def load_tailoring(cur, application_id: str) -> tuple[str, dict]:
    cur.execute("""SELECT id::text, evidence_map FROM generated_documents
                   WHERE application_id = %s AND doc_type = 'resume' AND qa_status = 'pass'
                   ORDER BY version DESC, created_at DESC LIMIT 1;""", (application_id,))
    row = cur.fetchone()
    if not row:
        raise ResumeTemplateError("No QA-passed resume exists for this application.")
    tailoring = (row[1] or {}).get("resume_template") or {}
    return row[0], tailoring


def register_artifact(cur, application_id: str, document_id: str, path: Path) -> None:
    """Register the exact local resume file allowed for a later upload action."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cur.execute(
        """
        INSERT INTO generated_document_artifacts
          (generated_document_id, application_id, artifact_type, file_path, filename, sha256)
        VALUES (%s, %s, 'resume', %s, %s, %s)
        ON CONFLICT (generated_document_id, artifact_type, sha256) DO UPDATE
        SET file_path = EXCLUDED.file_path, filename = EXCLUDED.filename;
        """,
        (document_id, application_id, str(path.resolve()), path.name, digest),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a verified JobOS resume into the fixed local Word template.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            document_id, tailoring = load_tailoring(cur, args.application_id)
    bullets = tailoring.get("project_bullets", [])
    skills = tailoring.get("skill_lines", [])
    subtitles = tailoring.get("project_subtitles", [])
    if not bullets:
        raise SystemExit("Verified resume has no template project bullets; regenerate it with the current generator.")
    destination = args.output_dir or OUTPUT_ROOT / args.application_id / document_id
    docx_path = destination / "resume.docx"
    try:
        render_docx(
            template=args.template.expanduser(), output=docx_path, project_bullets=bullets,
            skill_lines=skills, project_subtitles=subtitles,
        )
    except ResumeTemplateError as exc:
        raise SystemExit(f"Resume export blocked: {exc}") from exc
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            register_artifact(cur, args.application_id, document_id, docx_path)
    print(f"DOCX: {docx_path}\nNext: open this file in Word and Print/Save as PDF after review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
