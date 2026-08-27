"""Cycle-free materialization boundary for Human Review Hub rows.

This module owns only durable review-bundle/review-item materialization.  It
must not import approval executors, privileged browser actions, Gmail auth, or
the interactive review service; those higher-level modules may all depend on
this boundary without depending on each other.
"""
from __future__ import annotations

import hashlib
import json
from psycopg.types.json import Jsonb

from services.common.value_coercion import coerce_bool


class ReviewMaterializationError(RuntimeError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def ensure_review_bundle(cur, application_id: str, *, kind: str = "application") -> str:
    cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    if not row:
        raise ReviewMaterializationError(f"Application not found: {application_id}")
    title = f"{row[0] or 'Unknown company'} — {row[1] or 'Unknown role'}"
    cur.execute(
        """INSERT INTO review_bundles(application_id, bundle_kind, title, status)
           VALUES (%s, %s, %s, 'pending')
           ON CONFLICT (application_id, bundle_kind) DO UPDATE
             SET title = EXCLUDED.title, updated_at = now()
           RETURNING id::text;""",
        (application_id, kind, title),
    )
    return str(cur.fetchone()[0])


def ensure_approval_review_item(cur, approval_request_id: str) -> str | None:
    cur.execute(
        """SELECT ar.application_id::text, ar.type, ar.status, ar.summary_text,
                  ar.token_expires_at, a.company, a.job_title, ar.payload_json
             FROM approval_requests ar LEFT JOIN applications a ON a.id = ar.application_id
            WHERE ar.id = %s AND ar.status = 'pending'
              AND ar.token_expires_at > now();""",
        (approval_request_id,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    app_id, approval_type, _status, summary, expires, company, role, approval_payload = row
    bundle_id = ensure_review_bundle(cur, app_id)
    review_payload = {
        "approval_type": approval_type,
        "expires_at": expires.isoformat() if expires else None,
        "delegated_to_autofill": coerce_bool((approval_payload or {}).get("delegated_to_autofill")),
        "parent_approval_request_id": (approval_payload or {}).get("parent_approval_request_id"),
    }
    source_sha = _sha256_text(json.dumps({
        "approval_request_id": approval_request_id,
        "approval_type": approval_type,
        "payload": approval_payload or {},
    }, sort_keys=True, separators=(",", ":"), default=str))
    cur.execute(
        """INSERT INTO human_review_items(
               review_bundle_id, application_id, item_type, approval_request_id,
               title, summary_text, priority, payload_json, source_sha256)
           VALUES (%s, %s, 'approval_request', %s, %s, %s, 'urgent', %s, %s)
           ON CONFLICT (approval_request_id)
             WHERE approval_request_id IS NOT NULL AND item_type = 'approval_request' AND status = 'pending'
           DO UPDATE SET title = EXCLUDED.title, summary_text = EXCLUDED.summary_text,
                         payload_json = EXCLUDED.payload_json, source_sha256 = EXCLUDED.source_sha256,
                         updated_at = now()
           RETURNING id::text;""",
        (bundle_id, app_id, approval_request_id, f"Approval required: {approval_type}",
         summary or f"{company or ''} — {role or ''}", Jsonb(review_payload), source_sha),
    )
    return str(cur.fetchone()[0])
