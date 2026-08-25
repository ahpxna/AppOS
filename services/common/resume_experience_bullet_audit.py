"""JD-first audit for editable experience resume bullets.

Experience headers (employer, job title, dates, location) are immutable. Existing
experience bullet slots may be rewritten to better match the target JD and the
meaning of the immutable job title. A rewrite does not need a verbatim employment
quote from the official resume. However, precise technical/factual additions such
as named tools, metrics, credentials, quantified outcomes, or production ownership
still require approved user evidence when the rewrite relies on those specifics.
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
EXPERIENCE_HEADINGS = {
    "EXPERIENCE", "WORK EXPERIENCE", "PROFESSIONAL EXPERIENCE", "EMPLOYMENT EXPERIENCE",
}
STOP_HEADINGS = {"PROJECTS", "EDUCATION", "CERTIFICATIONS", "SKILLS"}
STOP_WORDS = {"and", "the", "for", "with", "from", "that", "this", "into", "using", "used", "was", "were", "are", "but", "not", "its", "their", "your", "via", "over", "under", "than", "then", "while"}


class ResumeExperienceAuditError(RuntimeError):
    """The baseline template cannot be used to audit experience bullets."""


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _heading_key(text: str) -> str:
    return normalize(text).upper().rstrip(":")


def _experience_bounds(paragraphs) -> tuple[int, int] | None:
    start = next((i for i, p in enumerate(paragraphs) if _heading_key(p.text) in EXPERIENCE_HEADINGS), None)
    if start is None:
        return None
    end = next(
        (i for i, p in enumerate(paragraphs[start + 1:], start + 1) if _heading_key(p.text) in STOP_HEADINGS),
        len(paragraphs),
    )
    return start, end


def experience_bullet_indices(paragraphs) -> list[int]:
    bounds = _experience_bounds(paragraphs)
    if bounds is None:
        return []
    start, end = bounds
    return [
        i for i, p in enumerate(paragraphs[start + 1:end], start + 1)
        if p.style.name == "List Paragraph" and normalize(p.text)
    ]


def load_template_experience_baselines(template: Path | None = None) -> dict[int, dict[str, Any]]:
    """Read immutable experience context plus the existing bullet text.

    ``header_context`` binds each editable bullet to the existing job block. The
    renderer never edits that header, so employer/title/date/location remain
    protected by the template integrity snapshot.
    """
    try:
        from docx import Document
    except ImportError as exc:
        raise ResumeExperienceAuditError("python-docx is required to read the resume template.") from exc
    path = Path(template or DEFAULT_TEMPLATE).expanduser()
    if not path.is_file():
        raise ResumeExperienceAuditError(f"Resume template not found: {path}")
    paragraphs = Document(path).paragraphs
    bounds = _experience_bounds(paragraphs)
    if bounds is None:
        return {}
    start, end = bounds
    result: dict[int, dict[str, Any]] = {}
    context: list[str] = []
    slot = 0
    for paragraph in paragraphs[start + 1:end]:
        text = normalize(paragraph.text)
        if not text:
            continue
        if paragraph.style.name == "List Paragraph":
            slot += 1
            previous = text
            result[slot] = {
                "slot": slot,
                "header_context": " | ".join(context[-3:]),
                "previous_bullet": previous,
                "max_chars": min(220, max(len(previous) + 40, int(len(previous) * 1.35))),
            }
        else:
            context.append(text)
    return result


def _changed_terms(before: str, after: str) -> set[str]:
    def tokenize(text: str) -> set[str]:
        return {
            word.strip(".,;:()[]{}").casefold()
            for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#/(),;:-]*", text)
            if word.strip(".,;:()[]{}")
        }
    return {word for word in tokenize(before) ^ tokenize(after) if len(word) >= 3 and word not in STOP_WORDS}


def validate_experience_bullet_change(
    change: Mapping[str, Any], *, baselines: Mapping[int, Mapping[str, Any]], jd_text: str,
    asset_source: str | None = None,
) -> list[str]:
    """Deterministically validate slot/header/JD binding and optional evidence.

    Unlike the previous contract, an experience rewrite is not required to cite a
    ``source_document_asset`` or a verbatim employment quote. The semantic auditor
    decides whether a no-source rewrite remains a plausible JD-first reframing of
    the existing job block. If an optional evidence quote is supplied, it must be
    present in the cited approved user asset.
    """
    problems: list[str] = []
    try:
        slot = int(change.get("slot"))
    except (TypeError, ValueError):
        return ["Experience bullet change has no valid fixed slot."]
    baseline = baselines.get(slot)
    if not baseline:
        return [f"Experience slot {slot} is not in the fixed template baseline."]
    before = normalize(change.get("previous_bullet"))
    after = normalize(change.get("text") or change.get("claim"))
    if before != normalize(baseline.get("previous_bullet")):
        problems.append("previous_bullet does not exactly match the fixed Word template experience bullet.")
    expected_context = normalize(baseline.get("header_context"))
    supplied_context = normalize(change.get("header_context"))
    if expected_context and supplied_context != expected_context:
        problems.append("header_context does not exactly match the immutable experience job block.")
    limit = int(baseline.get("max_chars") or 220)
    if not after or after == before:
        problems.append("Experience bullet must be a real, non-identical rewrite.")
    elif len(after) > limit:
        problems.append(f"Experience bullet exceeds its fixed {limit}-character layout budget.")
    jd_quote = normalize(change.get("jd_requirement_quote"))
    if len(jd_quote) < 8 or jd_quote.casefold() not in normalize(jd_text).casefold():
        problems.append("jd_requirement_quote is absent from the job description or too short.")

    evidence_quote = normalize(change.get("experience_evidence_quote"))
    if evidence_quote and asset_source is not None:
        if evidence_quote.casefold() not in normalize(asset_source).casefold():
            problems.append("experience_evidence_quote is absent from the cited approved user asset.")

    if len(normalize(change.get("why_better"))) < 24:
        problems.append("why_better must specifically explain the JD/job-title relevance gain.")
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
        named_terms.update(
            word.strip(".,;:()[]{}").casefold()
            for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#/(),;:-]*", old + " " + new)
            if word.strip(".,;:()[]{}")
        )
    uncovered = sorted(_changed_terms(before, after) - named_terms)
    if uncovered:
        problems.append("Changed terms missing from word_change_rationale: " + ", ".join(uncovered))
    return problems


def build_experience_bullet_audit_prompt(
    change: Mapping[str, Any], asset: Mapping[str, Any] | None, jd_text: str,
) -> str:
    """Audit JD-first experience reframing, with optional user evidence."""
    asset = dict(asset or {})
    source = "\n\n".join(str(asset.get(key) or "") for key in ("summary", "bullets", "positioning"))
    has_asset = bool(asset.get("id") or asset.get("title") or source.strip())
    asset_section = (
        f"""OPTIONAL APPROVED USER ASSET (may support precise technical/factual additions):
Title: {asset.get('title', '')}
Type: {asset.get('type', '')}
Source material:
{source}"""
        if has_asset else
        "OPTIONAL APPROVED USER ASSET: none. Evaluate this as a role-aligned rewrite only."
    )
    return f"""You are JobOS Resume Experience-Bullet Auditor V2.

POLICY
- Employer, job title, dates, location, and the job block are immutable.
- The bullet MAY be rewritten JD-first even without a verbatim employment quote,
  as long as it remains a plausible responsibility/result framing for the existing
  job title and does not contradict the original bullet.
- Prefer the target JD requirement and the existing job title over preserving the
  old bullet's exact wording. The old bullet is a scope anchor, not a phrase lock.
- General responsibility language, collaboration/communication framing, and
  role-relevant emphasis do NOT need a source asset.
- A NEW precise factual/technical assertion -- named tool/system/language,
  credential, metric, quantified outcome, production ownership, regulated scope,
  or other externally verifiable specific -- must be supported by the optional
  approved user asset and an exact evidence quote. If no such asset/quote exists,
  reject only that over-specific addition and suggest a more general JD-aligned
  rewrite.
- Reject a rewrite that changes the profession/seniority implied by the immutable
  title, invents a promotion/leadership event, or adds obviously incompatible work.

IMMUTABLE JOB BLOCK: {change.get('header_context', '')}
ORIGINAL BULLET: {change.get('previous_bullet', '')}
PROPOSED BULLET: {change.get('claim', change.get('text', ''))}
EXACT JD QUOTE: {change.get('jd_requirement_quote', '')}
OPTIONAL USER EVIDENCE QUOTE: {change.get('experience_evidence_quote', '')}
WORD-LEVEL CHANGE LOG: {change.get('word_change_rationale', [])}
WHY THIS IS BETTER: {change.get('why_better', '')}

FULL JOB DESCRIPTION:
{jd_text}

{asset_section}

Return ONLY valid JSON:
{{
  "verdict": "supported | overclaimed | unsupported | rule_violation",
  "reason": "specific approval or exact over-specific/incompatible change",
  "safe_rewrite": "empty when supported; otherwise a JD-aligned conservative alternative"
}}
"""
