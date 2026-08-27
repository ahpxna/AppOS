#!/usr/bin/env python3
"""Write the latest QA-passed tailoring into a local fixed-layout DOCX."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.canonical_resume_artifact_v1 import render_canonical_resume, ResumeTemplateError
from services.common.config import database_dsn
from services.common.artifact_registry_v1 import (
    begin_render_run, fail_render_run, finish_render_run, register_artifact as register_canonical_artifact,
)

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


def register_artifact(cur, application_id: str, document_id: str, path: Path) -> str:
    """Register the exact local resume file allowed for a later upload action."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cur.execute(
        """
        INSERT INTO generated_document_artifacts
          (generated_document_id, application_id, artifact_type, file_path, filename, sha256)
        VALUES (%s, %s, 'resume', %s, %s, %s)
        ON CONFLICT (generated_document_id, artifact_type, sha256) DO UPDATE
        SET file_path = EXCLUDED.file_path, filename = EXCLUDED.filename
        RETURNING id::text;
        """,
        (document_id, application_id, str(path.resolve()), path.name, digest),
    )
    generated_artifact_id = str(cur.fetchone()[0])
    artifact_id = register_canonical_artifact(
        cur, application_id=application_id, artifact_kind="resume_docx", path=path,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        provenance={"generated_document_id": document_id, "generated_document_artifact_id": generated_artifact_id},
    )
    cur.execute("UPDATE generated_document_artifacts SET artifact_id=%s WHERE id=%s;",
                (artifact_id, generated_artifact_id))
    return artifact_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a verified JobOS resume into the fixed local Word template.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            document_id, tailoring = load_tailoring(cur, args.application_id)
    destination = args.output_dir or OUTPUT_ROOT / args.application_id / document_id
    render_claim = begin_render_run(
        document_id=document_id, input_manifest={"document_id": document_id, "tailoring": tailoring},
        template=args.template, claimed_by="render-verified-resume-v1",
    )
    try:
        docx_path, pdf_path = render_canonical_resume(
            template=args.template, output_dir=destination, tailoring=tailoring
        )
    except ResumeTemplateError as exc:
        fail_render_run(render_claim, exc)
        raise SystemExit(f"Resume export blocked: {exc}") from exc
    except Exception as exc:
        fail_render_run(render_claim, exc, uncertain=True)
        raise
    # This utility still registers the editable DOCX for backwards-compatible
    # local use. Human Review registers/binds the canonical PDF separately.
    with psycopg.connect(database_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            docx_artifact_id = register_artifact(cur, args.application_id, document_id, docx_path)
            pdf_artifact_id = register_canonical_artifact(
                cur, application_id=args.application_id, artifact_kind="resume_pdf", path=pdf_path,
                mime_type="application/pdf", provenance={"generated_document_id": document_id},
            )
    finish_render_run(render_claim, docx_artifact_id=docx_artifact_id, pdf_artifact_id=pdf_artifact_id)
    print(f"DOCX: {docx_path}\nCANONICAL PDF: {pdf_path}\nHuman approval/upload must use the canonical PDF bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
