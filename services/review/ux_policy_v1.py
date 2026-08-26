"""Daily-use UX policy for the JobOS Human Review Hub.

This module intentionally does *not* weaken authorization.  It only decides how
already-materialized exact review items are presented and whether several
low-risk exact items may be approved by one explicit human gesture.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# These privileged actions have legal/account/security/navigation semantics and
# must never be hidden inside a batch-safe approval gesture.
NEVER_BATCH_APPROVAL_TYPES = {
    "privileged_submit_application",
    "privileged_accept_terms",
    "privileged_begin_application",
    "privileged_trust_external_domain",
    "privileged_choose_create_employer_account_path",
    "privileged_choose_navigation_target",
    "privileged_create_employer_account",
    "privileged_login_employer_account",
    "privileged_use_email_verification",
    "privileged_advance_application_step",
    "privileged_auth_manual_retry",
    "privileged_mfa_retry",
    "privileged_checkpoint_retry",
}

# Batch-safe means one human tap may approve several *exact* capabilities.  The
# underlying approval rows, hashes, one-shot execution and reconciliation rules
# stay independent.  Upload is safe here only when delegated to an exact parent
# autofill session; standalone uploads remain excluded.
BATCH_SAFE_APPROVAL_TYPES = {"autofill_form", "privileged_upload_document"}


def is_batch_safe_item(*, item_type: str, payload: dict[str, Any] | None) -> bool:
    payload = payload or {}
    if item_type == "document_review":
        return str(payload.get("qa_status") or "") == "pass"
    if item_type != "approval_request":
        return False
    approval_type = str(payload.get("approval_type") or "")
    if approval_type in NEVER_BATCH_APPROVAL_TYPES:
        return False
    if approval_type not in BATCH_SAFE_APPROVAL_TYPES:
        return False
    if approval_type == "privileged_upload_document":
        return bool(payload.get("delegated_to_autofill"))
    return True


def status_badges(envelope: dict[str, Any], *, reviewing_doc: str | None = None) -> str:
    documents = envelope.get("documents") if isinstance(envelope.get("documents"), dict) else {}
    job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
    form = envelope.get("form") if isinstance(envelope.get("form"), dict) else {}
    resume = "✅" if isinstance(documents.get("resume"), dict) else "—"
    cover = "✅" if isinstance(documents.get("cover_letter"), dict) else "—"
    # A document-review card describes a candidate artifact that is not yet an
    # approved application binding.  Calling it missing is misleading.
    if reviewing_doc == "resume":
        resume = "🟡 review"
    elif reviewing_doc == "cover_letter":
        cover = "🟡 review"
    step = str(job.get("current_step") or "")
    form_ready = "✅" if step in {"application_form_ready", "awaiting_approval", "autofill_executing", "form_filled", "application_ready", "submitted"} else "—"
    if isinstance(form, dict) and form.get("proposed_fields"):
        form_ready = "✅"
    return f"Resume {resume} · Cover letter {cover} · Form ready {form_ready}"


def _clean(text: Any, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def resume_change_lines(evidence_map: dict[str, Any] | None, *, limit: int = 8) -> list[str]:
    """Produce a human-first diff summary from verified structured tailoring.

    The canonical PDF remains the source of truth.  This is presentation only:
    it exposes exactly the structured changes that the canonical renderer uses.
    """
    tailoring = (evidence_map or {}).get("resume_template") if isinstance(evidence_map, dict) else {}
    tailoring = tailoring if isinstance(tailoring, dict) else {}
    lines: list[str] = []
    for item in tailoring.get("experience_bullets") or []:
        if not isinstance(item, dict):
            continue
        before, after = _clean(item.get("previous_bullet")), _clean(item.get("text"))
        slot = item.get("slot")
        if before:
            lines.append(f"Experience {slot}: − {before}")
        if after:
            lines.append(f"Experience {slot}: + {after}")
    for item in tailoring.get("project_bullets") or []:
        if not isinstance(item, dict):
            continue
        before = _clean(item.get("previous_bullet"))
        after = _clean(item.get("text"))
        slot = item.get("slot")
        if before:
            lines.append(f"Project {slot}: − {before}")
        if after:
            lines.append(f"Project {slot}: + {after}")
    for item in tailoring.get("skill_lines") or []:
        if isinstance(item, dict) and item.get("text"):
            lines.append(f"Skills: + {_clean(item.get('text'))}")
    for item in tailoring.get("project_subtitles") or []:
        if isinstance(item, dict) and item.get("text"):
            before = _clean(item.get("previous_subtitle"))
            after = _clean(item.get("text"))
            if before:
                lines.append(f"Project title {item.get('slot')}: − {before}")
            lines.append(f"Project title {item.get('slot')}: + {after}")
    if not lines:
        return ["No supported tailoring was needed; canonical baseline resume is unchanged."]
    if len(lines) > limit:
        return lines[:limit] + [f"… {len(lines) - limit} more verified change(s) in Details"]
    return lines


YES_NO_RE = re.compile(r"\b(yes\s*/?\s*no|are you|do you|have you|can you|will you|would you|willing to)\b", re.I)
SALARY_RE = re.compile(r"\b(expected|desired|target)\b.*\b(salary|compensation|pay)\b|\b(salary|compensation)\b.*\b(expect|desired|target)\b", re.I)


def quick_question_choices(question: str, *, salary_target: Any = None) -> list[str]:
    """Return conservative one-tap choices; empty means free text is required."""
    q = " ".join((question or "").split())
    if not q:
        return []
    if SALARY_RE.search(q) and salary_target not in (None, "", 0, 0.0):
        try:
            target = int(float(salary_target))
        except (TypeError, ValueError):
            target = 0
        if target > 0:
            return [f"${target:,}"]
    # Legal/work-authorization questions are deliberately filtered by the
    # review service before this helper is used.  For ordinary preference or
    # experience questions, Yes/No is a useful no-typing shortcut.
    if YES_NO_RE.search(q):
        return ["Yes", "No"]
    return []


def batch_snapshot(rows: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Canonical compact identity for a one-tap safe batch token."""
    result = []
    for row in rows:
        result.append({
            "item_id": str(row["item_id"]),
            "application_id": str(row["application_id"]),
            "source_sha256": str(row.get("source_sha256") or ""),
        })
    return sorted(result, key=lambda item: (item["application_id"], item["item_id"]))
