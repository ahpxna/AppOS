"""Evidence contract for the editable subtitle inside a fixed project header.

Only the text between ``Project name —`` and ``| GitHub`` can change. The Word
renderer separately preserves the name, hyperlink, date and paragraph styling.
Every subtitle change carries an exact JD quote, exact project-evidence quote,
and a visible rationale for every substantive changed term.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE = Path(os.getenv(
    "JOBOS_RESUME_TEMPLATE_PATH", REPO_ROOT / "data/resume-template/VU PHAN AN NGUYEN-official_For_all.docx"
)).expanduser()
HEADER_SLOTS = (1, 3, 5, 7, 9, 11)
STOP_WORDS = {"and", "the", "for", "with", "from", "that", "this", "into", "using", "used", "was", "were", "are", "but", "not", "its", "their", "your", "via", "over", "under", "than", "then", "while"}


class ResumeHeaderAuditError(RuntimeError):
    """The supplied template cannot safely expose a subtitle slot."""


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def subtitle_bounds(header_text: str) -> tuple[int, int]:
    """Return the content range after the em dash and before the GitHub separator."""
    start = header_text.find("—")
    end = header_text.find("|", start + 1)
    if start < 0 or end < 0 or end <= start:
        raise ResumeHeaderAuditError("Project header must contain ‘— subtitle | GitHub’.")
    start += 1
    while start < len(header_text) and header_text[start].isspace():
        start += 1
    while end > start and header_text[end - 1].isspace():
        end -= 1
    if start == end:
        raise ResumeHeaderAuditError("Project header subtitle is empty.")
    return start, end


def subtitle_from_header(header_text: str) -> str:
    start, end = subtitle_bounds(header_text)
    return header_text[start:end]


def load_template_subtitle_baselines(template: Path | None = None) -> dict[int, str]:
    """Read the six existing project subtitles in fixed resume slot order."""
    try:
        from docx import Document
    except ImportError as exc:
        raise ResumeHeaderAuditError("python-docx is required to read the resume template.") from exc
    path = Path(template or DEFAULT_TEMPLATE).expanduser()
    if not path.is_file():
        raise ResumeHeaderAuditError(f"Resume template not found: {path}")
    paragraphs = Document(path).paragraphs
    try:
        project_index = next(i for i, p in enumerate(paragraphs) if normalize(p.text).upper() == "PROJECTS")
        certification_index = next(i for i, p in enumerate(paragraphs) if normalize(p.text).upper() == "CERTIFICATIONS")
    except StopIteration as exc:
        raise ResumeHeaderAuditError("Resume template is missing PROJECTS or CERTIFICATIONS heading.") from exc
    headers = [
        p for p in paragraphs[project_index + 1:certification_index]
        if "—" in p.text and "|" in p.text and "github" in p.text.casefold()
    ]
    if len(headers) != len(HEADER_SLOTS):
        raise ResumeHeaderAuditError(f"Expected 6 project headers, found {len(headers)}.")
    return {slot: normalize(subtitle_from_header(header.text)) for slot, header in zip(HEADER_SLOTS, headers)}


def changed_terms(before: str, after: str) -> set[str]:
    tokenize = lambda text: {word.casefold() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#/-]*", text)}
    return {word for word in tokenize(before) ^ tokenize(after) if len(word) >= 3 and word not in STOP_WORDS}


def validate_subtitle_change(
    change: Mapping[str, Any], *, baseline_subtitles: Mapping[int, str], jd_text: str,
    asset_source: str | None = None,
) -> list[str]:
    """Return deterministic audit violations; semantic approval is done by LLM QA."""
    problems: list[str] = []
    try:
        slot = int(change.get("slot"))
    except (TypeError, ValueError):
        return ["Subtitle change has no valid fixed project slot."]
    before, after = normalize(change.get("previous_subtitle")), normalize(change.get("text") or change.get("claim"))
    if slot not in baseline_subtitles:
        problems.append(f"Slot {slot} is not an editable fixed project header.")
    elif before != normalize(baseline_subtitles[slot]):
        problems.append("previous_subtitle does not exactly match the fixed Word template.")
    if not after or after == before:
        problems.append("Subtitle must be a real, non-identical change from the template baseline.")
    if len(after) > 88:
        problems.append("Subtitle exceeds the fixed one-line 88-character budget.")
    jd_quote = normalize(change.get("jd_requirement_quote"))
    if len(jd_quote) < 8 or jd_quote.casefold() not in normalize(jd_text).casefold():
        problems.append("jd_requirement_quote is absent from the job description or too short.")
    evidence_quote = normalize(change.get("project_evidence_quote"))
    if len(evidence_quote) < 8:
        problems.append("project_evidence_quote is required and must be specific.")
    elif asset_source is not None and evidence_quote.casefold() not in normalize(asset_source).casefold():
        problems.append("project_evidence_quote is absent from the cited project asset.")
    if len(normalize(change.get("why_better"))) < 24:
        problems.append("why_better must specifically explain the JD/project relevance gain.")
    word_changes = change.get("word_change_rationale")
    if not isinstance(word_changes, list) or not word_changes:
        return problems + ["word_change_rationale must explain each substantive changed term."]
    named_terms: set[str] = set()
    for item in word_changes:
        if not isinstance(item, dict):
            problems.append("Each word_change_rationale entry must be an object.")
            continue
        old, new, reason = normalize(item.get("before")), normalize(item.get("after")), normalize(item.get("why"))
        if not (old or new) or len(reason) < 16:
            problems.append("Each word change needs before/after text and a concrete reason.")
        named_terms.update(word.casefold() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#/-]*", old + " " + new))
    uncovered = sorted(changed_terms(before, after) - named_terms)
    if uncovered:
        problems.append("Changed terms missing from word_change_rationale: " + ", ".join(uncovered))
    return problems


def build_subtitle_audit_prompt(change: Mapping[str, Any], asset: Mapping[str, Any], jd_text: str) -> str:
    source = "\n\n".join(str(asset.get(key) or "") for key in ("summary", "bullets", "positioning"))
    return f"""You are JobOS Resume Header-Subtitle Auditor.

Assess only the text between the fixed project name and the fixed GitHub link.
Approve it only if every change is factually supported by the cited project
asset and materially improves relevance to the exact quoted JD requirement.
Reject keyword stuffing, cosmetic rewrites, scope inflation, or a rationale
that does not justify each substantive word change. Project name, link and date
are immutable and not editable.

ORIGINAL SUBTITLE: {change.get('previous_subtitle', '')}
PROPOSED SUBTITLE: {change.get('claim', change.get('text', ''))}
EXACT JD QUOTE: {change.get('jd_requirement_quote', '')}
EXACT PROJECT EVIDENCE QUOTE: {change.get('project_evidence_quote', '')}
WORD-LEVEL CHANGE LOG: {change.get('word_change_rationale', [])}
WHY THIS IS BETTER: {change.get('why_better', '')}

FULL JOB DESCRIPTION:
{jd_text}

ONLY PROJECT ASSET:
Title: {asset.get('title', '')}
Type: {asset.get('type', '')}
Source material:
{source}

Return ONLY valid JSON:
{{
  "verdict": "supported | overclaimed | unsupported | rule_violation",
  "reason": "specific approval or exact unsupported/irrelevant subtitle change",
  "safe_rewrite": "empty when supported; otherwise a conservative alternative"
}}
"""
