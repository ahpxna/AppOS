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

from services.control_plane.review_materialization import ensure_approval_review_item

PRIVILEGED_TYPES = {
    "privileged_begin_application",
    "privileged_trust_external_domain",
    "privileged_choose_create_employer_account_path",
    "privileged_choose_navigation_target",
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


def _authorization_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Drop presentation-only context from the authorization/idempotency digest."""
    return {key: value for key, value in body.items() if key not in {"binding_sha256", "review_context"}}


def create_privileged_request(cur, *, application_id: str, action_type: str,
                              payload: dict[str, Any], summary: str,
                              requested_by: str = "jobos", ttl_minutes: int = 60) -> str:
    if action_type not in PRIVILEGED_TYPES:
        raise RuntimeError(f"unsupported privileged action: {action_type}")
    cur.execute("SELECT company, job_title, coalesce(job_url,''), coalesce(jd_hash,''), current_step, pipeline_version FROM applications WHERE id = %s;", (application_id,))
    app = cur.fetchone()
    if not app:
        raise RuntimeError("application not found")
    body = dict(payload or {})
    body.update({
        "action_type": action_type, "company": app[0], "job_title": app[1],
        "application_id": application_id, "job_url": str(app[2] or ""),
        "jd_hash": str(app[3] or ""), "expected_application_step": str(app[4] or ""),
        "expected_pipeline_version": int(app[5] or 0),
    })
    body["binding_sha256"] = _hash_json(_authorization_payload(body))
    idem = _hash_json({"type": action_type, "application_id": application_id,
                       "binding_sha256": body["binding_sha256"]})
    # A time-expired pending/approved row still participates in the partial
    # unique index until its status changes. Retire it before the active lookup.
    cur.execute(
        """UPDATE approval_requests SET status='expired', executing_task_id=NULL
              WHERE idempotency_key=%s AND status IN ('pending','approved')
                AND token_expires_at <= now();""",
        (idem,),
    )
    cur.execute(
        """SELECT id::text FROM approval_requests
            WHERE idempotency_key = %s AND status IN ('pending','approved','executing')
              AND (status='executing' OR token_expires_at > now())
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
               max_attempts, idempotency_key, target_action,
               parent_approval_request_id,bound_pipeline_version,bound_autofill_plan_key,
               binding_sha256,expected_target_id,application_job_url,application_jd_hash,
               bound_email_candidate_id,bound_autofill_plan_id,created_at)
           VALUES (%s,%s,%s,'pending','telegram',%s,
                   now() + make_interval(mins => %s),%s,%s,1,%s,%s,
                   %s,%s,%s,%s,%s,%s,%s,%s,%s,now())
           ON CONFLICT (idempotency_key)
             WHERE idempotency_key IS NOT NULL AND status IN ('pending','approved','executing')
           DO NOTHING
           RETURNING id::text;""",
        (action_type, application_id, Jsonb(body), token_hash, ttl_minutes,
         requested_by, summary, idem, action_type,
         body.get("parent_approval_request_id"), body.get("expected_pipeline_version"),
         body.get("autofill_plan_key"), body.get("binding_sha256"),
         body.get("expected_target_id") or body.get("target_id"), body.get("job_url"), body.get("jd_hash"),
         body.get("candidate_id"), body.get("autofill_plan_id")),
    )
    inserted = cur.fetchone()
    if inserted:
        request_id = str(inserted[0])
    else:
        # Concurrent materializers race safely: reuse the unique-index winner.
        cur.execute(
            """SELECT id::text FROM approval_requests
                  WHERE idempotency_key=%s AND status IN ('pending','approved','executing')
                  ORDER BY created_at DESC LIMIT 1;""",
            (idem,),
        )
        winner = cur.fetchone()
        if not winner:
            raise RuntimeError("privileged approval materialization raced without a reusable winner")
        return str(winner[0])
    cur.execute(
        """INSERT INTO approval_events(approval_request_id, event, actor, detail_json, binding_sha256)
           VALUES (%s,'created',%s,%s,%s);""",
        (request_id, requested_by, Jsonb({"action_type": action_type, "binding_sha256": body["binding_sha256"]}),
         body["binding_sha256"]),
    )
    ensure_approval_review_item(cur, request_id)
    return request_id
