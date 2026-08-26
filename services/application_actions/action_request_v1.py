"""Create exact, one-shot privileged application action approvals.

These approvals are separate from normal autofill. They cover navigation or
account/legal side effects such as opening Apply, creating/logging into an
employer account, accepting consent text, using an email verification token,
and final Submit. Telegram approval expresses human intent; the executor still
revalidates exact browser/document bindings immediately before I/O.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from psycopg.types.json import Jsonb

PRIVILEGED_TYPES = {
    "privileged_begin_application",
    "privileged_trust_external_domain",
    "privileged_create_employer_account",
    "privileged_login_employer_account",
    "privileged_use_email_verification",
    "privileged_accept_terms",
    "privileged_upload_document",
    "privileged_advance_application_step",
    "privileged_auth_manual_retry",
    "privileged_mfa_retry",
    "privileged_checkpoint_retry",
    "privileged_submit_application",
}


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def create_privileged_request(cur, *, application_id: str, action_type: str,
                              payload: dict[str, Any], summary: str,
                              requested_by: str = "jobos", ttl_minutes: int = 60) -> str:
    if action_type not in PRIVILEGED_TYPES:
        raise RuntimeError(f"unsupported privileged action: {action_type}")
    cur.execute("SELECT company, job_title, coalesce(job_url,''), coalesce(jd_hash,''), current_step FROM applications WHERE id = %s;", (application_id,))
    app = cur.fetchone()
    if not app:
        raise RuntimeError("application not found")
    body = dict(payload or {})
    body.update({
        "action_type": action_type, "company": app[0], "job_title": app[1],
        "application_id": application_id, "job_url": str(app[2] or ""),
        "jd_hash": str(app[3] or ""), "expected_application_step": str(app[4] or ""),
    })
    body["binding_sha256"] = _hash_json({key: value for key, value in body.items() if key != "binding_sha256"})
    idem = _hash_json({"type": action_type, "application_id": application_id,
                       "binding_sha256": body["binding_sha256"]})
    cur.execute(
        """SELECT id::text FROM approval_requests
            WHERE idempotency_key = %s AND status IN ('pending','approved')
              AND token_expires_at > now()
            ORDER BY created_at DESC LIMIT 1;""",
        (idem,),
    )
    row = cur.fetchone()
    if row:
        return str(row[0])
    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    cur.execute(
        """INSERT INTO approval_requests(
               type, application_id, payload_json, status, approval_channel,
               approval_token_hash, token_expires_at, requested_by, summary_text,
               max_attempts, idempotency_key, target_action, created_at)
           VALUES (%s,%s,%s,'pending','telegram',%s,
                   now() + make_interval(mins => %s),%s,%s,1,%s,%s,now())
           RETURNING id::text;""",
        (action_type, application_id, Jsonb(body), token_hash, ttl_minutes,
         requested_by, summary, idem, action_type),
    )
    request_id = str(cur.fetchone()[0])
    cur.execute(
        """INSERT INTO approval_events(approval_request_id, event, actor, detail_json)
           VALUES (%s,'created',%s,%s);""",
        (request_id, requested_by, Jsonb({"action_type": action_type, "binding_sha256": body["binding_sha256"]})),
    )
    from services.review.review_service_v1 import ensure_approval_review
    ensure_approval_review(cur, request_id)
    return request_id
