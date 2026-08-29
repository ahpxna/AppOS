"""Canonical fixed-template resume artifact rendering.

The structured QA payload is interpreted exactly once here. Every product path
that emits a resume for human review or later upload uses the same DOCX renderer
and the same PDF export. The PDF returned by this module is the canonical JobOS
resume artifact: human review binds its SHA-256 and browser upload reuses those
exact bytes.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RENDERER_PATH = ROOT / "services" / "document-generation" / "resume_template_renderer.py"
_SPEC = importlib.util.spec_from_file_location("jobos_canonical_resume_renderer", RENDERER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Cannot load resume renderer: {RENDERER_PATH}")
renderer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(renderer)
ResumeTemplateError = renderer.ResumeTemplateError


def render_canonical_resume(*, template: Path, output_dir: Path,
                            tailoring: dict[str, Any]) -> tuple[Path, Path]:
    """Render one structured QA payload into the exact review/upload PDF.

    Empty edit lists mean "preserve the fixed-template baseline". They are not
    an error and are intentionally distinct from an unsupported/tampered edit,
    which the underlying fixed-template renderer still rejects.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    docx = output_dir / "resume.docx"
    renderer.render_docx(
        template=template.expanduser(),
        output=docx,
        experience_bullets=list(tailoring.get("experience_bullets") or []),
        project_bullets=list(tailoring.get("project_bullets") or []),
        skill_lines=list(tailoring.get("skill_lines") or []),
        project_subtitles=list(tailoring.get("project_subtitles") or []),
    )
    overlay_enabled = os.getenv("JOBOS_RESUME_TEMPLATE_OVERLAY_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"}
    reference_raw = os.getenv("JOBOS_RESUME_REFERENCE_PDF_PATH", "").strip()
    reference_pdf = Path(reference_raw).expanduser() if reference_raw else template.expanduser().with_suffix(".pdf")
    # The reference-PDF overlay currently owns project-bullet + skill slots.
    # Experience/subtitle edits still require the full DOCX->PDF renderer; do
    # not silently drop those edits merely because overlay mode is enabled.
    overlay_compatible = not list(tailoring.get("experience_bullets") or []) and not list(tailoring.get("project_subtitles") or [])
    if overlay_enabled and overlay_compatible:
        pdf = renderer.export_pdf_from_reference(
            reference_pdf=reference_pdf, output_pdf=output_dir / "resume.pdf",
            project_bullets=list(tailoring.get("project_bullets") or []),
            skill_lines=list(tailoring.get("skill_lines") or []),
        )
    else:
        pdf = renderer.export_pdf(docx, output_dir)
    return docx, pdf
