"""One approved input snapshot for preview, approval, and deterministic writes.

This is the single mapping from approved JobOS values to the narrow profile
given to the form planner.  Preview, approval, and execution therefore see
the same uploads, legal answers, remembered answers, and input hash.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from services.common.autofill_field_registry import AUTOFILL_FIELD_REGISTRY
from services.common.autofill_identity import autofill_input_hash
from services.common.immigration_semantics import EXACT_CANDIDATE_ADDITIONAL_CLASSES
from services.common.question_memory import normalize_question


class AutofillContextError(RuntimeError):
    """The approved context cannot safely be reconstructed."""


@dataclass(frozen=True)
class AutofillContext:
    profile: dict[str, Any]
    sensitive_answers: dict[str, Any]
    remembered_answers: dict[str, Any]
    input_hash: str


def _load_remembered_answers(cur, application_id: str) -> dict[str, dict[str, str]]:
    cur.execute("SELECT lower(coalesce(company, '')), coalesce(ats_type, '') FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    if not row:
        return {}
    company, ats_type = row
    cur.execute(
        """SELECT question_normalized, answer_text, answer_kind
             FROM application_question_memory
            WHERE (scope = 'global')
               OR (scope = 'ats' AND ats_type = %s)
               OR (scope = 'company' AND company_normalized = %s)
            ORDER BY CASE scope WHEN 'company' THEN 3 WHEN 'ats' THEN 2 ELSE 1 END DESC,
                     updated_at DESC;""",
        (ats_type, normalize_question(company)),
    )
    answers: dict[str, dict[str, str]] = {}
    for question, answer, answer_kind in cur.fetchall():
        answers.setdefault(str(question), {"value": str(answer), "answer_kind": str(answer_kind)})
    return answers


def load_autofill_context(
    cur, *, application_id: str, artifact_binding: Mapping[str, Any] | None,
    document_sha256: str, page_url: str, page_fingerprint_sha256: str,
    data_root: Path,
) -> AutofillContext:
    """Load exactly the approved values authorized for one application."""
    cur.execute("SELECT field_name, field_value FROM v_autofill_ready_values;")
    ready = {str(name): str(value) for name, value in cur.fetchall()
             if str(value).strip() and str(value) != "FILL_ME"}
    profile: dict[str, Any] = {
        "personal": {}, "address": {}, "education": {}, "employment": {},
        "preferences": {}, "documents": {}, "_approval_ready_values": ready,
    }
    for source, target in AUTOFILL_FIELD_REGISTRY.items():
        if source in ready:
            profile[target[0]][target[1]] = ready[source]
    personal = profile["personal"]
    if personal.get("first_name") and personal.get("last_name"):
        personal["full_name"] = f"{personal['first_name']} {personal['last_name']}"

    artifact = artifact_binding or {}
    if artifact.get("artifact_id"):
        cur.execute(
            """SELECT gd.doc_type, gda.file_path, gda.filename, gda.sha256
                 FROM generated_document_artifacts gda
                 JOIN generated_documents gd ON gd.id = gda.generated_document_id
                WHERE gda.id = %s AND gda.application_id = %s
                  AND gd.application_id = %s AND gd.qa_status = 'pass' AND gd.approved = true;""",
            (artifact["artifact_id"], application_id, application_id),
        )
        row = cur.fetchone()
        if not row:
            raise AutofillContextError("The approved upload artifact no longer belongs to this verified application document.")
        doc_type, file_path, filename, digest = row
        if str(digest) != str(artifact.get("artifact_sha256")) or str(filename) != str(artifact.get("artifact_filename")):
            raise AutofillContextError("The approved upload artifact changed after approval; issue a new approval.")
        path = Path(str(file_path)).expanduser().resolve()
        root = data_root.resolve()
        if not path.is_file() or root not in path.parents:
            raise AutofillContextError("Approved upload artifact is outside the managed JobOS data directory.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != str(digest):
            raise AutofillContextError("Approved upload artifact bytes changed after approval.")
        if str(doc_type) not in {"resume", "cover_letter"} or path.name != str(filename):
            raise AutofillContextError("Approved upload artifact has an unsupported document type or filename.")
        profile["documents"][str(doc_type)] = str(path)

    cur.execute(
        """SELECT current_work_authorization, requires_sponsorship_to_start,
                  requires_future_sponsorship, us_citizen, us_person,
                  permanent_work_authorization, user_confirmed_at, confirmation_version
             FROM immigration_profiles WHERE profile_key = 'primary';"""
    )
    row = cur.fetchone()
    sensitive: dict[str, Any] = {}
    if row and row[6] and int(row[7] or 0) >= 1:
        for key, value in zip(("CURRENT_AUTHORIZATION", "SPONSORSHIP_TO_START", "SPONSORSHIP_NOW_OR_FUTURE",
                               "US_CITIZENSHIP", "US_PERSON", "PERMANENT_WORK_AUTHORIZATION"), row[:6]):
            if str(value).casefold() in {"yes", "no"}:
                sensitive[key] = {"value": str(value).title(), "confirmed_at": str(row[6]),
                                  "confirmation_version": int(row[7])}
    exact_classes = tuple(item.value for item in EXACT_CANDIDATE_ADDITIONAL_CLASSES)
    cur.execute(
        """SELECT field_name, answer, updated_at FROM sensitive_answers
             WHERE approved_by_user = true AND field_name = ANY(%s);""",
        ([f"immigration:{item}" for item in exact_classes],),
    )
    for field_name, answer, updated_at in cur.fetchall():
        question_class = str(field_name).removeprefix("immigration:")
        if question_class in exact_classes and str(answer).casefold() in {"yes", "no"}:
            sensitive[question_class] = {"value": str(answer).title(), "confirmed_at": str(updated_at),
                                         "confirmation_version": 1}
    remembered = _load_remembered_answers(cur, application_id)

    return AutofillContext(
        profile,
        sensitive,
        remembered,
        autofill_input_hash(
            profile=profile,
            sensitive_answers=sensitive,
            remembered_answers=remembered,
            document_sha256=document_sha256,
            artifact_sha256=artifact.get("artifact_sha256"),
            page_url=page_url,
            page_fingerprint_sha256=page_fingerprint_sha256,
        ),
    )
