#!/usr/bin/env python3
"""Render physical QA-passed document artifacts for human review.

No LLM is called. Resume rendering reuses the fixed-template renderer and its
one-page PDF validation. Cover-letter rendering writes the already truth-checked
content verbatim into a deterministic PDF.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env
from services.common.artifact_registry_v1 import (
    begin_render_run, fail_render_run, finish_render_run, register_artifact as register_canonical_artifact,
)

load_repo_env()
OUTPUT_ROOT = Path(os.getenv("JOBOS_REVIEW_ARTIFACT_DIR", ROOT / "data/review-artifacts"))
TEMPLATE = Path(os.getenv("JOBOS_RESUME_TEMPLATE_PATH", ROOT / "data/resume-template/VU PHAN AN NGUYEN-official_For_all.docx"))
from services.common.canonical_resume_artifact_v1 import render_canonical_resume


class ReviewArtifactError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _register(cur, *, document_id: str, application_id: str, doc_type: str, path: Path) -> str:
    cur.execute(
        """INSERT INTO generated_document_artifacts(
               generated_document_id, application_id, artifact_type, file_path, filename, sha256)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (generated_document_id, artifact_type, sha256) DO UPDATE
             SET file_path = EXCLUDED.file_path, filename = EXCLUDED.filename
           RETURNING id::text;""",
        (document_id, application_id, doc_type, str(path.resolve()), path.name, _sha(path)),
    )
    generated_artifact_id = str(cur.fetchone()[0])
    artifact_id = register_canonical_artifact(
        cur, application_id=application_id, artifact_kind=f"{doc_type}_review_pdf", path=path,
        mime_type="application/pdf",
        provenance={"generated_document_id": document_id, "generated_document_artifact_id": generated_artifact_id},
    )
    cur.execute("UPDATE generated_document_artifacts SET artifact_id=%s WHERE id=%s;",
                (artifact_id, generated_artifact_id))
    return artifact_id


def _plain_cover_letter_pdf(content: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    body.fontName = "Helvetica"
    body.fontSize = 10.5
    body.leading = 14
    story = []
    # Preserve words exactly; only escape markup and convert paragraph breaks
    # into PDF layout primitives.
    import html
    paragraphs = re.split(r"\n\s*\n", content.strip())
    for para in paragraphs:
        text = html.escape(" ".join(para.splitlines()).strip())
        if text:
            story.append(Paragraph(text, body))
            story.append(Spacer(1, 0.12 * inch))
    doc = SimpleDocTemplate(str(output), pagesize=LETTER, rightMargin=0.7 * inch,
                            leftMargin=0.7 * inch, topMargin=0.65 * inch,
                            bottomMargin=0.65 * inch)
    doc.build(story)


def render_document_pdf(cur, document_id: str) -> Path:
    cur.execute(
        """SELECT application_id::text, doc_type, content, evidence_map, qa_status
             FROM generated_documents WHERE id = %s;""",
        (document_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ReviewArtifactError("Generated document not found.")
    application_id, doc_type, content, evidence_map, qa_status = row
    if doc_type not in {"resume", "cover_letter"}:
        raise ReviewArtifactError("Only resume/cover_letter documents have review PDFs.")
    if qa_status != "pass":
        raise ReviewArtifactError("Review PDF is emitted only after QA pass.")
    out_dir = OUTPUT_ROOT / application_id / document_id
    input_manifest = {"document_id": document_id, "doc_type": doc_type, "content": content or "",
                      "evidence_map": evidence_map or {}}
    render_claim = begin_render_run(
        document_id=document_id, input_manifest=input_manifest,
        template=(TEMPLATE if doc_type == "resume" else None), claimed_by="render-review-artifacts-v1",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_artifact_id = None
    try:
        if doc_type == "resume":
            tailoring = (evidence_map or {}).get("resume_template") or {}
            docx, pdf = render_canonical_resume(
                template=TEMPLATE, output_dir=out_dir, tailoring=tailoring
            )
            docx_artifact_id = register_canonical_artifact(
                cur, application_id=application_id, artifact_kind="resume_docx", path=docx,
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                provenance={"generated_document_id": document_id, "render_run_id": render_claim.run_id},
            )
        else:
            pdf = out_dir / "cover_letter.pdf"
            _plain_cover_letter_pdf(content or "", pdf)
        pdf_artifact_id = _register(cur, document_id=document_id, application_id=application_id,
                                    doc_type=doc_type, path=pdf)
        finish_render_run(render_claim, docx_artifact_id=docx_artifact_id,
                          pdf_artifact_id=pdf_artifact_id)
        return pdf
    except Exception as exc:
        fail_render_run(render_claim, exc, uncertain=bool(out_dir.exists()))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Render JobOS QA-passed review PDFs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--document-id")
    group.add_argument("--application-id")
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        if args.document_id:
            paths = [render_document_pdf(cur, args.document_id)]
        else:
            cur.execute(
                """SELECT id::text FROM generated_documents
                    WHERE application_id = %s AND doc_type IN ('resume','cover_letter')
                      AND qa_status = 'pass' AND approved = false
                    ORDER BY doc_type, version DESC, created_at DESC;""",
                (args.application_id,),
            )
            seen = set(); ids = []
            for (document_id,) in cur.fetchall():
                cur.execute("SELECT doc_type FROM generated_documents WHERE id = %s", (document_id,))
                doc_type = cur.fetchone()[0]
                if doc_type not in seen:
                    seen.add(doc_type); ids.append(document_id)
            paths = [render_document_pdf(cur, document_id) for document_id in ids]
        conn.commit()
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
