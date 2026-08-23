"""Strict JD-and-profile-evidence audit for editable project resume bullets."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE = Path(os.getenv(
    "JOBOS_RESUME_TEMPLATE_PATH", REPO_ROOT / "data/resume-template/VU PHAN AN NGUYEN-official_For_all.docx"
)).expanduser()
STOP_WORDS = {"and", "the", "for", "with", "from", "that", "this", "into", "using", "used", "was", "were", "are", "but", "not", "its", "their", "your", "via", "over", "under", "than", "then", "while"}


class ResumeBulletAuditError(RuntimeError):
    """The baseline template cannot be used to audit project bullets."""


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _is_header(paragraph) -> bool:
    text = paragraph.text.casefold()
    return "—" in paragraph.text and "|" in paragraph.text and "github" in text


def load_template_bullet_baselines(template: Path | None = None) -> dict[int, str]:
    """Read the original 12 fixed project bullet texts without modifying them."""
    try:
        from docx import Document
    except ImportError as exc:
        raise ResumeBulletAuditError("python-docx is required to read the resume template.") from exc
    path = Path(template or DEFAULT_TEMPLATE).expanduser()
    if not path.is_file():
        raise ResumeBulletAuditError(f"Resume template not found: {path}")
    paragraphs = Document(path).paragraphs
    try:
        projects = next(i for i, p in enumerate(paragraphs) if normalize(p.text).upper() == "PROJECTS")
        certifications = next(i for i, p in enumerate(paragraphs) if normalize(p.text).upper() == "CERTIFICATIONS")
    except StopIteration as exc:
        raise ResumeBulletAuditError("Resume template is missing PROJECTS or CERTIFICATIONS heading.") from exc
    bullets = [p for p in paragraphs[projects + 1:certifications] if p.style.name == "List Paragraph" and not _is_header(p)]
    if len(bullets) != 12:
        raise ResumeBulletAuditError(f"Expected 12 fixed project bullets, found {len(bullets)}.")
    return {index: normalize(paragraph.text) for index, paragraph in enumerate(bullets, start=1)}


def changed_terms(before: str, after: str) -> set[str]:
    tokenize = lambda text: {word.casefold() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#/-]*", text)}
    return {word for word in tokenize(before) ^ tokenize(after) if len(word) >= 3 and word not in STOP_WORDS}


def validate_bullet_change(
    change: Mapping[str, Any], *, baseline_bullets: Mapping[int, str], jd_text: str,
    asset_source: str | None = None,
) -> list[str]:
    """Return structural violations; semantic relevance is checked by verifier LLM."""
    problems: list[str] = []
    try:
        slot = int(change.get("slot"))
    except (TypeError, ValueError):
        return ["Project bullet change has no valid fixed slot."]
    before, after = normalize(change.get("previous_bullet")), normalize(change.get("text") or change.get("claim"))
    if slot not in baseline_bullets:
        problems.append(f"Slot {slot} is not in the fixed project-bullet baseline.")
    elif before != normalize(baseline_bullets[slot]):
        problems.append("previous_bullet does not exactly match the fixed Word template.")
    limit = 200 if slot % 2 else 105
    if not after or after == before:
        problems.append("Project bullet must be a real, non-identical change from the template baseline.")
    elif len(after) > limit:
        problems.append(f"Project bullet exceeds its fixed {limit}-character budget.")
    jd_quote = normalize(change.get("jd_requirement_quote"))
    if len(jd_quote) < 8 or jd_quote.casefold() not in normalize(jd_text).casefold():
        problems.append("jd_requirement_quote is absent from the job description or too short.")
    evidence_quote = normalize(change.get("project_evidence_quote"))
    if len(evidence_quote) < 8:
        problems.append("project_evidence_quote is required and must be specific.")
    elif asset_source is not None and evidence_quote.casefold() not in normalize(asset_source).casefold():
        problems.append("project_evidence_quote is absent from the cited profile asset.")
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


def build_bullet_audit_prompt(change: Mapping[str, Any], asset: Mapping[str, Any], jd_text: str) -> str:
    source = "\n\n".join(str(asset.get(key) or "") for key in ("summary", "bullets", "positioning"))
    return f"""You are JobOS Resume Project-Bullet Auditor.

Approve a bullet rewrite only if it is fully supported by this one project asset
and directly addresses the exact quoted JD requirement. Reject unsupported
tools, inflated scope, generic keyword stuffing, or creative claims. The
candidate is allowed to position genuine academic/project work strongly, but
never to fabricate employment or project facts.

ORIGINAL BULLET: {change.get('previous_bullet', '')}
PROPOSED BULLET: {change.get('claim', change.get('text', ''))}
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
  "reason": "specific approval or exact unsupported/irrelevant change",
  "safe_rewrite": "empty when supported; otherwise a conservative alternative"
}}
"""
