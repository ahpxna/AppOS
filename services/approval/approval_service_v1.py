"""
L1 -- APPROVAL SERVICE

Issues and redeems single-use, expiring tokens for the human gates in the
pipeline. This is the component that unblocks L7: browser_queue_worker refuses
fill_application_form unless a valid approval_request exists.

Token handling:
  The plaintext token is generated, shown once, and never written anywhere.
  Only its sha256 hash is stored. Reading the database therefore does not let
  anyone approve anything, and a token pasted into a chat log can be revoked
  by expiring the request rather than by rotating a shared secret.

  Redemption is single-use and constant-time compared. An unmatched token does
  not mutate unrelated approvals; local rate limits belong outside this table.

Usage:
  python services/approval/approval_service_v1.py create \
      --application-id <uuid> --type submit_application --ttl-minutes 60
  python services/approval/approval_service_v1.py list
  python services/approval/approval_service_v1.py approve --token <token>
  python services/approval/approval_service_v1.py deny --token <token> --note "..."
  python services/approval/approval_service_v1.py expire-stale
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn, load_repo_env
from services.common.autofill_identity import canonical_page_url
from services.common.autofill_action_scope import autofill_plan_key
from services.autofill.autofill_context_v1 import load_autofill_context
from services.control_plane.pipeline_state import DEFAULT_PIPELINE_STATE_STORE, PipelineStateError

load_repo_env()

DSN = database_dsn()

SERVICE_VERSION = "approval_service_v2_capability_bound_2026_08_23"

APPROVAL_TYPES = (
    "submit_application",   # human attestation for a final submission; JobOS never submits
    "send_message",         # L8: send a reply to a recruiter
    "spend_over_budget",    # L1: exceed the daily cost budget
    "browser_login",        # L3: open a session on a site requiring login
    "fit_review",           # L5: borderline fit score (60-75), ask before spending on it
    "autofill_form",        # one exact document/origin form-write capability
)

DEFAULT_TTL_MINUTES = 60


def _resolve_fit_review_transition(cur, *, application_id: str | None, request_id: str,
                                   approved: bool, actor: str) -> None:
    """Consume a borderline-fit decision in the same transaction as redemption.

    The orchestrator intentionally cannot claim human-gated rows, so putting
    this transition behind a later orchestrator pass creates a permanent
    liveness hole.  A stale application makes the entire redemption rollback;
    an approval row can never be committed without its matching state event.
    """
    if not application_id:
        raise RuntimeError("fit_review is missing its application binding")
    target = "fit_analyzed" if approved else "fit_rejected"
    try:
        # Lock before changing the capability.  The ensuing transition is
        # therefore inseparable from the redemption: a competing lifecycle
        # actor cannot leave a terminal approval attached to a stale app.
        cur.execute("SELECT current_step FROM applications WHERE id=%s FOR UPDATE;", (application_id,))
        row = cur.fetchone()
        if not row or str(row[0]) != "awaiting_fit_review":
            raise PipelineStateError("application is no longer awaiting this fit review")
        DEFAULT_PIPELINE_STATE_STORE.transition(
            cur, application_id=application_id, expected_from="awaiting_fit_review",
            to=target, actor=actor,
            reason="Human resolved the borderline fit review.",
            detail={"approval_request_id": request_id, "decision": "approved" if approved else "denied"},
            required_kind="human",
        )
    except PipelineStateError as exc:
        raise RuntimeError(f"fit review is stale or cannot transition: {exc}") from exc


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def fetch_submit_binding(cur, application_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT id::text, doc_type, version, content, qa_status, approved
        FROM generated_documents
        WHERE application_id = %s
          AND qa_status = 'pass'
          AND approved = true
        ORDER BY doc_type, version, created_at;
        """,
        (application_id,),
    )
    rows = cur.fetchall()
    if not rows:
        raise RuntimeError(
            "ERROR: no document has passed the truth checker for this application."
        )

    documents = [
        {
            "id": row[0],
            "doc_type": row[1],
            "version": row[2],
            "qa_status": row[4],
            "approved": bool(row[5]),
            "content_hash": hashlib.sha256((row[3] or "").encode("utf-8")).hexdigest(),
        }
        for row in rows
    ]
    return {
        "documents": documents,
        "content_hash": hash_json(documents),
    }


def fetch_document_binding(cur, application_id: str, document_id: str) -> Dict[str, Any]:
    """Return one QA-passed, user-approved document for one application."""
    cur.execute(
        """
        SELECT gd.id::text, gd.doc_type, gd.version, gd.content, gd.source_jd_hash
        FROM generated_documents gd JOIN applications a ON a.id = gd.application_id
        WHERE gd.id = %s AND gd.application_id = %s
          AND qa_status = 'pass' AND approved = true;
        """,
        (document_id, application_id),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(
            "document must belong to this application and have passed QA and user approval."
        )
    cur.execute("SELECT jd_hash FROM applications WHERE id = %s;", (application_id,))
    app_row = cur.fetchone()
    if not row[4] or not app_row or row[4] != app_row[0]:
        raise RuntimeError("Approved document was generated for a different JD version; regenerate and re-review it.")
    return {
        "id": row[0], "doc_type": row[1], "version": row[2],
        "content_hash": hashlib.sha256((row[3] or "").encode("utf-8")).hexdigest(),
        "source_jd_hash": row[4],
    }


def fetch_artifact_binding(cur, application_id: str, document_id: str, artifact_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT id::text, sha256, filename
        FROM generated_document_artifacts
        WHERE id = %s AND application_id = %s AND generated_document_id = %s;
        """,
        (artifact_id, application_id, document_id),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("artifact must belong to the exact application and approved document.")
    return {"id": row[0], "sha256": row[1], "filename": row[2]}


def current_autofill_input_hash(cur, *, application_id: str, artifact_binding: Dict[str, Any],
                                document_sha256: str, page_url: str, page_fingerprint: str) -> str:
    """Use the exact context shared by preview and execution."""
    return load_autofill_context(
        cur, application_id=application_id, artifact_binding=artifact_binding,
        document_sha256=document_sha256, page_url=page_url,
        page_fingerprint_sha256=page_fingerprint,
        data_root=Path(__file__).resolve().parents[2] / "data",
    ).input_hash


def normalise_origin(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("--expected-origin must be an http(s) origin, e.g. https://jobs.example.com")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def fetch_reply_binding(cur, reply_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT id::text, thread_id::text, subject, body_text, evidence_map,
               asset_ids_used, qa_status
        FROM drafted_replies
        WHERE id = %s;
        """,
        (reply_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Drafted reply not found: {reply_id}")
    reply = {
        "id": row[0],
        "thread_id": row[1],
        "subject": row[2] or "",
        "body_text": row[3] or "",
        "evidence_map": row[4] or {},
        "asset_ids_used": row[5] or [],
        "qa_status": row[6],
    }
    return {
        "reply": reply,
        "content_hash": hash_json({
            "subject": reply["subject"],
            "body_text": reply["body_text"],
            "evidence_map": reply["evidence_map"],
            "asset_ids_used": reply["asset_ids_used"],
        }),
    }


def assert_binding_matches(cur, request_row: Dict[str, Any]) -> None:
    atype = request_row["type"]
    payload = request_row.get("payload_json") or {}
    application_id = request_row.get("application_id")
    if atype == "submit_application":
        if not application_id:
            raise RuntimeError("Approval request missing application_id.")
        binding = fetch_submit_binding(cur, application_id)
        if payload.get("content_hash") != binding["content_hash"]:
            raise RuntimeError(
                "Submission approval no longer matches the approved document set. "
                "Recreate the approval request for the latest content."
            )
    elif atype == "send_message":
        reply_id = (payload or {}).get("drafted_reply_id")
        if not reply_id:
            raise RuntimeError("send_message approval payload missing drafted_reply_id.")
        binding = fetch_reply_binding(cur, reply_id)
        if binding["reply"].get("qa_status") != "pass":
            raise RuntimeError(
                "Reply has not passed the truth checker yet. Verify it before approval."
            )
        if payload.get("content_hash") != binding["content_hash"]:
            raise RuntimeError(
                "Reply approval no longer matches the drafted reply content. "
                "Recreate the approval request."
            )
    elif atype == "autofill_form":
        if not application_id:
            raise RuntimeError("Autofill approval request missing application_id.")
        cur.execute("SELECT coalesce(job_url,''), coalesce(jd_hash,''), current_step FROM applications WHERE id=%s;",
                    (application_id,))
        app = cur.fetchone()
        if not app:
            raise RuntimeError("Autofill application no longer exists.")
        if str(payload.get("application_job_url") or "") != str(app[0] or ""):
            raise RuntimeError("Autofill approval job URL changed after preview; prepare a fresh plan.")
        if str(payload.get("application_jd_hash") or "") != str(app[1] or ""):
            raise RuntimeError("Autofill approval JD changed after preview; prepare a fresh plan.")
        if str(payload.get("expected_application_step") or "") != str(app[2] or ""):
            raise RuntimeError("Autofill approval pipeline step changed; prepare a fresh plan.")
        if not str(payload.get("expected_target_id") or "").strip():
            raise RuntimeError("Autofill approval predates exact browser-target binding; prepare a fresh plan.")
        document_id = payload.get("document_id")
        if not document_id:
            raise RuntimeError("Autofill approval payload missing document_id.")
        binding = fetch_document_binding(cur, application_id, document_id)
        if payload.get("document_sha256") != binding["content_hash"]:
            raise RuntimeError("Autofill approval no longer matches the exact approved document.")
        artifact_id = payload.get("artifact_id")
        if artifact_id:
            artifact = fetch_artifact_binding(cur, application_id, document_id, str(artifact_id))
            if artifact["sha256"] != payload.get("artifact_sha256") or artifact["filename"] != payload.get("artifact_filename"):
                raise RuntimeError("Autofill approval no longer matches the exact upload artifact.")
        current_hash = current_autofill_input_hash(
            cur,
            application_id=application_id,
            artifact_binding={
                "artifact_id": artifact_id,
                "artifact_sha256": payload.get("artifact_sha256"),
                "artifact_filename": payload.get("artifact_filename"),
            } if artifact_id else {},
            document_sha256=binding["content_hash"],
            page_url=str(payload.get("expected_initial_url") or ""),
            page_fingerprint=str(payload.get("expected_page_fingerprint") or ""),
        )
        if current_hash != payload.get("autofill_input_hash"):
            raise RuntimeError("Autofill inputs or JD changed after preview; prepare and approve a fresh plan.")
        current_plan_key = autofill_plan_key(
            application_id=application_id,
            page_url=str(payload.get("expected_initial_url") or ""),
            page_fingerprint=str(payload.get("expected_page_fingerprint") or ""),
            input_hash=current_hash, action_scope=payload.get("autofill_action_scope") or {},
        )
        if current_plan_key != str(payload.get("autofill_plan_key") or ""):
            raise RuntimeError("Autofill plan binding changed after preview; prepare a fresh plan.")


def log_event(cur, request_id: Optional[str], event: str,
              actor: str, detail: Optional[Dict[str, Any]] = None) -> None:
    cur.execute(
        """
        INSERT INTO approval_events (approval_request_id, event, actor, detail_json)
        VALUES (%s, %s, %s, %s);
        """,
        (request_id, event, actor, Jsonb(detail or {})),
    )


# ---------------------------------------------------------------- autofill parent/child gating

def _normalise_expected_uploads(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise RuntimeError("Expected upload capabilities must be a JSON list.")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("Expected upload capability entries must be objects.")
        spec = {key: str(item.get(key) or "") for key in ("field_ref", "document_type", "artifact_id", "sha256")}
        if not all(spec.values()) or len(spec["sha256"]) != 64:
            raise RuntimeError("Expected upload capabilities require exact field/document/artifact/SHA bindings.")
        ident = tuple(spec[key] for key in ("field_ref", "document_type", "artifact_id", "sha256"))
        if ident not in seen:
            seen.add(ident); result.append(spec)
    return sorted(result, key=lambda item: (item["field_ref"], item["document_type"], item["artifact_id"], item["sha256"]))


def _queue_autofill_task(cur, *, request_id: str, application_id: str, payload: dict[str, Any], actor: str) -> bool:
    cur.execute(
        """INSERT INTO browser_tasks
             (task_type, requested_by, application_id, status, priority,
              input_json, approval_request_id, expected_origin, generated_document_id,
              document_sha256, timeout_seconds, bound_artifact_id, artifact_sha256,
              artifact_filename, expected_initial_url, expected_page_fingerprint,
              autofill_input_hash, autofill_action_scope, idempotency_key, created_at)
           VALUES ('fill_application_form', %s, %s, 'queued', 'high', '{}'::jsonb,
                   %s, %s, %s, %s, 300, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (approval_request_id) WHERE approval_request_id IS NOT NULL DO NOTHING;""",
        (actor, application_id, request_id, payload["expected_origin"], payload["document_id"],
         payload["document_sha256"], payload.get("artifact_id"), payload.get("artifact_sha256"),
         payload.get("artifact_filename"), payload["expected_initial_url"],
         payload["expected_page_fingerprint"], payload["autofill_input_hash"],
         Jsonb(payload.get("autofill_action_scope") or {}), f"autofill:{request_id}"),
    )
    if cur.rowcount == 1:
        log_event(cur, request_id, "autofill_task_queued", actor,
                  {"expected_origin": payload["expected_origin"], "document_id": payload["document_id"],
                   "autofill_plan_key": payload.get("autofill_plan_key")})
        return True
    return False


def _repair_delegated_children_for_parent(cur, *, application_id: str, parent_request_id: str, payload: dict[str, Any]) -> list[str]:
    """Deterministically materialize any missing upload children for one exact parent.

    Parent creation and child creation historically used separate transactions.  The
    parent payload therefore carries the exact child packages so any later decision,
    sync, or queue attempt can repair an interrupted materialization without reusing
    a child from a different parent session.
    """
    raw_packages = payload.get("delegated_upload_packages") or []
    if not isinstance(raw_packages, list):
        raise RuntimeError("delegated_upload_packages must be a list")
    created: list[str] = []
    for raw in raw_packages:
        if not isinstance(raw, dict):
            raise RuntimeError("delegated upload package must be an object")
        package = dict(raw)
        package["parent_approval_request_id"] = str(parent_request_id)
        package["delegated_to_autofill"] = True
        required = ("field_ref", "document_type", "artifact_id", "sha256", "autofill_plan_key")
        if not all(str(package.get(key) or "") for key in required):
            raise RuntimeError("delegated upload package is missing an exact binding")
        from services.application_actions.action_request_v1 import create_privileged_request
        rid = create_privileged_request(
            cur, application_id=application_id, action_type="privileged_upload_document",
            payload=package,
            summary=(f"Upload exact approved {package['document_type']} {package.get('filename')!r} "
                     f"to field {package.get('field_label') or package['field_ref']!r} for parent {parent_request_id}."),
            requested_by="autofill-parent-repair",
        )
        created.append(str(rid))
    return created


def queue_ready_autofill_for_plan(cur, *, application_id: str, plan_key: str, actor: str) -> bool:
    """Repair exact children, then queue only the approved parent they belong to."""
    if not application_id or not plan_key:
        return False
    cur.execute(
        """SELECT id::text, payload_json, status FROM approval_requests
              WHERE application_id=%s AND type='autofill_form'
                AND payload_json->>'autofill_plan_key'=%s
              ORDER BY created_at DESC LIMIT 1 FOR UPDATE;""",
        (application_id, plan_key),
    )
    parent = cur.fetchone()
    if not parent:
        return False
    request_id, raw_payload, parent_status = str(parent[0]), dict(parent[1] or {}), str(parent[2] or "")

    # Self-heal the historical parent-commit/child-materialization crash window.
    if parent_status in {"pending", "approved"}:
        _repair_delegated_children_for_parent(
            cur, application_id=application_id, parent_request_id=request_id, payload=raw_payload,
        )

    cur.execute(
        """UPDATE approval_requests SET status='expired'
              WHERE application_id=%s AND type='privileged_upload_document'
                AND payload_json->>'parent_approval_request_id'=%s
                AND payload_json->>'delegated_to_autofill'='true'
                AND status='pending' AND token_expires_at <= now();""",
        (application_id, request_id),
    )
    if parent_status in {"denied", "expired"}:
        _close_delegated_children_for_parent(
            cur, application_id=application_id, plan_key=plan_key,
            parent_request_id=request_id,
            reason=f"Parent autofill approval is {parent_status}; delegated upload capability closed.",
        )
        return False
    if parent_status != "approved":
        return False

    cur.execute(
        """SELECT ar.id::text, ar.payload_json, ar.status, pae.status
              FROM approval_requests ar
              LEFT JOIN privileged_action_executions pae ON pae.approval_request_id=ar.id
             WHERE ar.application_id=%s AND ar.type='privileged_upload_document'
               AND ar.payload_json->>'parent_approval_request_id'=%s
               AND ar.payload_json->>'delegated_to_autofill'='true';""",
        (application_id, request_id),
    )
    children = [(str(r[0]), dict(r[1] or {}), str(r[2] or ""), str(r[3] or "")) for r in cur.fetchall()]
    expected = _normalise_expected_uploads(raw_payload.get("expected_upload_capabilities") or [])
    terminal_resolved = {"approved", "denied", "expired", "consumed"}
    for spec in expected:
        matches = [(rid, child, status, exec_status) for rid, child, status, exec_status in children
                   if all(str(child.get(key) or "") == spec[key]
                          for key in ("field_ref", "document_type", "artifact_id", "sha256"))]
        if len(matches) != 1 or matches[0][2] not in terminal_resolved:
            return False
        if matches[0][3] == "needs_reconciliation":
            return False
    assert_binding_matches(cur, {"type": "autofill_form", "application_id": application_id,
                                 "payload_json": raw_payload, "status": "approved"})
    return _queue_autofill_task(cur, request_id=request_id, application_id=application_id,
                                payload=raw_payload, actor=actor)


def _close_delegated_children_for_parent(cur, *, application_id: str, plan_key: str, reason: str, parent_request_id: str | None = None) -> None:
    if not application_id or not plan_key:
        return
    if parent_request_id:
        cur.execute(
            """UPDATE approval_requests
                  SET status='expired', executing_task_id=NULL,
                      action_note=COALESCE(action_note,%s)
                WHERE application_id=%s AND type='privileged_upload_document'
                  AND payload_json->>'parent_approval_request_id'=%s
                  AND payload_json->>'delegated_to_autofill'='true'
                  AND status IN ('pending','approved');""",
            (reason[:500], application_id, parent_request_id),
        )
    else:
        cur.execute(
            """UPDATE approval_requests
                  SET status='expired', executing_task_id=NULL,
                      action_note=COALESCE(action_note,%s)
                WHERE application_id=%s AND type='privileged_upload_document'
                  AND payload_json->>'autofill_plan_key'=%s
                  AND payload_json->>'delegated_to_autofill'='true'
                  AND status IN ('pending','approved');""",
            (reason[:500], application_id, plan_key),
        )


def _restore_autofill_ready_after_terminal_parent(cur, *, application_id: str, plan_key: str, reason: str, parent_request_id: str | None = None) -> None:
    """CAS-safe recovery when an autofill approval ends before browser I/O."""
    _close_delegated_children_for_parent(cur, application_id=application_id, plan_key=plan_key, reason=reason, parent_request_id=parent_request_id)
    try:
        DEFAULT_PIPELINE_STATE_STORE.transition(
            cur, application_id=application_id, expected_from="awaiting_approval",
            to="application_form_ready", actor="approval-service", reason=reason[:500],
            detail={"autofill_plan_key": plan_key}, required_kind="recovery",
            guard_sql="""NOT EXISTS (
                SELECT 1 FROM approval_requests ar
                 WHERE ar.application_id=applications.id AND ar.type='autofill_form'
                   AND ar.status IN ('pending','approved','executing')
                   AND ar.token_expires_at > now()
            )""",
        )
    except PipelineStateError:
        # A surviving capability or concurrent lifecycle owner deliberately
        # keeps the form in its current state; no synthetic event is emitted.
        return


def _reject_email_candidate_for_denied_request(cur, *, application_id: str | None,
                                               atype: str, payload: dict[str, object]) -> None:
    """Persist a human denial so Gmail watcher cannot rematerialize the same secret.

    Both direct USE EMAIL denial and denial of a Gmail magic-link trust gate are
    durable candidate rejections. Employer-domain trust unrelated to Gmail is
    unaffected.
    """
    if not application_id or not isinstance(payload, dict):
        return
    candidate_id = str(payload.get("candidate_id") or "")
    if not candidate_id:
        return
    is_use = atype == "privileged_use_email_verification"
    is_magic_trust = (atype == "privileged_trust_external_domain"
                      and str(payload.get("trust_source") or "") == "gmail_magic_link")
    if not (is_use or is_magic_trust):
        return
    cur.execute(
        """UPDATE email_verification_candidates
              SET status='rejected'
            WHERE id=%s AND application_id=%s
              AND status IN ('discovered','approved');""",
        (candidate_id, application_id),
    )


# ---------------------------------------------------------------- create

def cmd_create(conn, args) -> int:
    with conn.cursor() as cur:
        # Expire time-invalid capabilities before the active idempotency lookup.
        # Otherwise an approved/pending row whose token TTL already elapsed can
        # be returned as the "existing" approval and block a fresh capability.
        cur.execute(
            """UPDATE approval_requests
                  SET status = 'expired', executing_task_id = NULL
                WHERE status IN ('pending', 'approved')
                  AND token_expires_at <= now();"""
        )
        if args.application_id:
            cur.execute(
                "SELECT company, job_title, current_step, coalesce(job_url,''), coalesce(jd_hash,'') FROM applications WHERE id = %s;",
                (args.application_id,),
            )
            row = cur.fetchone()
            if not row:
                print(f"ERROR: application not found: {args.application_id}")
                return 1
            company, job_title, step, application_job_url, application_jd_hash = row
            summary = args.summary or (
                f"{args.type}: {company} / {job_title} (currently at {step})"
            )
        else:
            company = job_title = step = application_job_url = application_jd_hash = None
            summary = args.summary or args.type

        payload = {"company": company, "job_title": job_title,
                   "service_version": SERVICE_VERSION}
        if args.application_id:
            payload["application_job_url"] = str(application_job_url or "")
            payload["application_jd_hash"] = str(application_jd_hash or "")
        if getattr(args, "review_context_json", None):
            try:
                review_context = json.loads(args.review_context_json)
            except json.JSONDecodeError as exc:
                print(f"ERROR: --review-context-json is invalid JSON: {exc}")
                return 1
            if not isinstance(review_context, dict):
                print("ERROR: --review-context-json must be a JSON object.")
                return 1
            payload["review_context"] = review_context
        idempotency_key = None

        # Only issue an approval when there is something concrete to approve.
        if args.type == "submit_application" and args.application_id:
            try:
                binding = fetch_submit_binding(cur, args.application_id)
            except RuntimeError as e:
                print(f"ERROR: {e}")
                return 1
            payload["content_hash"] = binding["content_hash"]
            payload["documents"] = binding["documents"]
            idempotency_key = hash_json({
                "type": args.type,
                "application_id": args.application_id,
                "content_hash": binding["content_hash"],
            })
        elif args.type == "autofill_form":
            if not all((args.application_id, args.document_id, args.expected_origin, args.expected_target_id,
                        args.expected_page_url, args.expected_page_fingerprint, args.expected_autofill_input_hash,)):
                print("ERROR: autofill_form requires --application-id, --document-id, --expected-origin, --expected-target-id, --expected-page-url, --expected-page-fingerprint and --expected-autofill-input-hash.")
                return 1
            try:
                binding = fetch_document_binding(cur, args.application_id, args.document_id)
                expected_origin = normalise_origin(args.expected_origin)
                expected_page_url = canonical_page_url(args.expected_page_url)
                if normalise_origin(expected_page_url) != expected_origin:
                    raise RuntimeError("--expected-page-url must belong to --expected-origin.")
                if len(args.expected_page_fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in args.expected_page_fingerprint.casefold()):
                    raise RuntimeError("--expected-page-fingerprint must be a SHA-256 hex value from the read-only page identity command.")
                artifact = (fetch_artifact_binding(cur, args.application_id, args.document_id, args.artifact_id)
                            if args.artifact_id else None)
                action_scope = json.loads(args.autofill_action_scope_json or "{}")
                if (not isinstance(action_scope, dict) or int(action_scope.get("version") or 0) != 3
                        or not isinstance(action_scope.get("actions"), list)):
                    raise RuntimeError("--autofill-action-scope-json must contain exact version=3 actions from jobos autofill prepare.")
                for item in action_scope["actions"]:
                    if (not isinstance(item, dict) or item.get("action") not in {"fill", "select", "check", "upload"}
                            or not item.get("ref") or not item.get("value_sha256")):
                        raise RuntimeError("autofill action scope contains a non-exact action; prepare a fresh plan.")
                input_hash = current_autofill_input_hash(
                    cur,
                    application_id=args.application_id,
                    artifact_binding={
                        "artifact_id": artifact["id"],
                        "artifact_sha256": artifact["sha256"],
                        "artifact_filename": artifact["filename"],
                    } if artifact else {},
                    document_sha256=binding["content_hash"],
                    page_url=expected_page_url,
                    page_fingerprint=args.expected_page_fingerprint.casefold(),
                )
                expected_input_hash = args.expected_autofill_input_hash.casefold()

                if (
                    len(expected_input_hash) != 64
                    or any(
                        ch not in "0123456789abcdef"
                        for ch in expected_input_hash
                    )
                ):
                    raise RuntimeError(
                        "--expected-autofill-input-hash must be a SHA-256 "
                        "hex value emitted by jobos autofill prepare."
                    )

                if input_hash != expected_input_hash:
                    raise RuntimeError(
                        "Autofill inputs changed after preview; "
                        "run jobos autofill prepare again."
                    )
                if str(step or "") != "application_form_ready":
                    raise RuntimeError(
                        f"Autofill approval can only be created from application_form_ready; current step is {step!r}."
                    )
                expected_plan_key = autofill_plan_key(
                    application_id=args.application_id, page_url=expected_page_url,
                    page_fingerprint=args.expected_page_fingerprint.casefold(),
                    input_hash=input_hash, action_scope=action_scope,
                )
                if str(args.autofill_plan_key or "").casefold() != expected_plan_key:
                    raise RuntimeError("--autofill-plan-key does not match the exact current form plan.")
                expected_upload_capabilities = _normalise_expected_uploads(
                    json.loads(args.expected_upload_capabilities_json or "[]")
                )
                delegated_upload_packages = json.loads(args.delegated_upload_packages_json or "[]")
                if not isinstance(delegated_upload_packages, list):
                    raise RuntimeError("--delegated-upload-packages-json must be a JSON list.")
            except (RuntimeError, json.JSONDecodeError) as exc:
                print(f"ERROR: {exc}")
                return 1
            payload.update({
                "document_id": binding["id"],
                "document_sha256": binding["content_hash"],
                "expected_origin": expected_origin,
                "expected_target_id": str(args.expected_target_id),
                "expected_initial_url": expected_page_url,
                "expected_page_fingerprint": args.expected_page_fingerprint.casefold(),
                "autofill_input_hash": input_hash,
                "artifact_id": artifact["id"] if artifact else None,
                "artifact_sha256": artifact["sha256"] if artifact else None,
                "artifact_filename": artifact["filename"] if artifact else None,
                "autofill_action_scope": action_scope,
                "autofill_plan_key": expected_plan_key,
                "expected_upload_capabilities": expected_upload_capabilities,
                "delegated_upload_packages": delegated_upload_packages,
                # Creation normalizes application_form_ready -> awaiting_approval.
                # Bind redemption/execution to the post-creation authoritative step.
                "expected_application_step": "awaiting_approval",
            })
            idempotency_key = hash_json({
                "type": args.type, "application_id": args.application_id,
                "document_id": binding["id"], "document_sha256": binding["content_hash"],
                "expected_origin": expected_origin, "expected_target_id": str(args.expected_target_id),
                "expected_initial_url": expected_page_url,
                "expected_page_fingerprint": args.expected_page_fingerprint.casefold(), "autofill_input_hash": input_hash,
                "artifact_id": artifact["id"] if artifact else None,
                "artifact_sha256": artifact["sha256"] if artifact else None,
                "autofill_action_scope": action_scope,
                "autofill_plan_key": expected_plan_key,
                "expected_upload_capabilities": expected_upload_capabilities,
                "delegated_upload_packages": delegated_upload_packages,
                "application_job_url": str(application_job_url or ""),
                "application_jd_hash": str(application_jd_hash or ""),
            })
        elif args.type == "fit_review" and args.application_id:
            payload["content_hash"] = hash_json({
                "application_id": args.application_id,
                "summary": summary,
            })
            idempotency_key = hash_json({
                "type": args.type,
                "application_id": args.application_id,
                "summary": summary,
            })

        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)

        if idempotency_key:
            cur.execute(
                """
                SELECT id::text, status, summary_text
                FROM approval_requests
                WHERE idempotency_key = %s
                  AND status IN ('pending', 'approved', 'executing')
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing:
                print(f"\n  existing request: {existing[0]}")
                print(f"  status:          {existing[1]}")
                print(f"  summary:          {existing[2]}")
                return 0

        cur.execute(
            """
            INSERT INTO approval_requests
              (type, application_id, payload_json, status, approval_channel,
               approval_token_hash, token_expires_at, requested_by, summary_text,
               max_attempts, idempotency_key, target_action, bound_document_id,
               bound_document_sha256, expected_origin, bound_artifact_id,
               bound_artifact_sha256, bound_artifact_filename, expected_initial_url,
               expected_page_fingerprint, bound_autofill_input_hash, bound_autofill_action_scope, created_at)
            VALUES (%s, %s, %s, 'pending', %s, %s,
                    now() + make_interval(mins => %s), %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (idempotency_key)
              WHERE idempotency_key IS NOT NULL
                AND status IN ('pending', 'approved', 'executing')
            DO NOTHING
            RETURNING id::text, token_expires_at;
            """,
            (
                args.type, args.application_id, Jsonb(payload),
                args.channel, token_hash, args.ttl_minutes,
                args.requested_by, summary, args.max_attempts, idempotency_key,
                "fill_application_form" if args.type == "autofill_form" else None,
                payload.get("document_id"), payload.get("document_sha256"), payload.get("expected_origin"),
                payload.get("artifact_id"), payload.get("artifact_sha256"), payload.get("artifact_filename"),
                payload.get("expected_initial_url"), payload.get("expected_page_fingerprint"), payload.get("autofill_input_hash"),
                Jsonb(payload.get("autofill_action_scope") or {}),
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """SELECT id::text, status, summary_text, token_expires_at
                     FROM approval_requests
                    WHERE idempotency_key=%s
                      AND status IN ('pending','approved','executing')
                    ORDER BY created_at DESC LIMIT 1;""",
                (idempotency_key,),
            )
            winner = cur.fetchone()
            if not winner:
                raise RuntimeError("approval materialization raced without a reusable winner")
            conn.commit()
            print(f"\n  existing request: {winner[0]}")
            print(f"  status:          {winner[1]}")
            print(f"  summary:         {winner[2]}")
            return 0
        request_id, expires = inserted
        log_event(cur, request_id, "created", args.requested_by,
                  {"type": args.type, "ttl_minutes": args.ttl_minutes})

        # The privileged application-handoff flow marks an authenticated page
        # as application_form_ready. The frozen browser worker completes its
        # existing state transition only from awaiting_approval, so normalize
        # this one new handoff state here when the exact autofill capability is
        # actually created. Existing flows already at awaiting_approval remain
        # unchanged.
        if args.apply and args.type == "autofill_form" and args.application_id:
            DEFAULT_PIPELINE_STATE_STORE.transition(
                cur, application_id=args.application_id, expected_from="application_form_ready",
                to="awaiting_approval", actor=args.requested_by,
                reason="Exact form packaged for human autofill approval.",
                detail={"approval_request_id": request_id}, required_kind="automated",
            )

        if not args.apply:
            conn.rollback()
            print("\nDRY RUN. No approval created.")
            return 0

        conn.commit()

        print(f"\n  request id: {request_id}")
        print(f"  type:       {args.type}")
        print(f"  summary:    {summary}")
        print(f"  expires:    {expires}")
        print("\n  TOKEN (shown once, not stored anywhere):")
        print(f"  {token}")
        print("\n  Redeem with:")
        print(f"    python services/approval/approval_service_v1.py approve --token {token}")
        return 0


def _stop_application_for_denied_privileged(cur, *, application_id: str | None,
                                              atype: str, payload: dict[str, Any], actor: str) -> bool:
    """Make the daily-UX ❌ decision durable for application-level gates.

    Exact email candidates, delegated uploads, autofill plans, and navigation
    alternatives are capability-level decisions and therefore do not abandon
    the whole application. Employer/app progression gates do.
    """
    if not application_id:
        return False
    stop_types = {
        'privileged_begin_application', 'privileged_create_employer_account',
        'privileged_login_employer_account', 'privileged_choose_create_employer_account_path',
        'privileged_auth_manual_retry', 'privileged_mfa_retry', 'privileged_checkpoint_retry',
        'privileged_accept_terms', 'privileged_advance_application_step',
        'privileged_submit_application',
    }
    if atype == 'privileged_trust_external_domain':
        if str(payload.get('trust_source') or '') == 'gmail_magic_link':
            return False
        stop_types.add(atype)
    if atype not in stop_types:
        return False
    cur.execute('SELECT current_step FROM applications WHERE id=%s FOR UPDATE;', (application_id,))
    row = cur.fetchone()
    if not row or str(row[0] or '') == 'abandoned':
        return False
    current = str(row[0] or '')
    cur.execute("SELECT 1 FROM pipeline_transitions WHERE from_step=%s AND to_step='abandoned';", (current,))
    if not cur.fetchone():
        return False
    try:
        DEFAULT_PIPELINE_STATE_STORE.transition(
            cur, application_id=application_id, expected_from=current, to="abandoned",
            actor=actor, status="abandoned",
            reason="Human stopped the application by denying an application-level privileged gate.",
            detail={"approval_type": atype}, required_kind="human",
        )
    except PipelineStateError:
        return False
    cur.execute(
        """UPDATE approval_requests SET status='expired',executing_task_id=NULL,
                  action_note=coalesce(action_note,'') || ' Application stopped by human.'
              WHERE application_id=%s AND id <> coalesce(%s::uuid,id)
                AND status IN ('pending','approved') AND consumed_at IS NULL;""",
        (application_id, str(payload.get('_request_id') or '') or None),
    )
    return True


# ---------------------------------------------------------------- redeem

def redeem(conn, token: str, *, decision: str, note: str, actor: str) -> int:
    token_hash = hash_token(token)

    with conn.cursor() as cur:
        # Expire anything past its TTL before matching, so a stale token can
        # never be redeemed by racing the clock.
        cur.execute(
            """
            UPDATE approval_requests
            SET status = 'expired'
            WHERE status IN ('pending', 'approved') AND token_expires_at <= now();
            """
        )

        cur.execute(
            """
            SELECT id::text, type, application_id::text, status,
                   approval_token_hash, token_expires_at,
                   attempt_count, max_attempts, summary_text
            FROM approval_requests
            WHERE status = 'pending'
            ORDER BY created_at DESC;
            """
        )
        rows = cur.fetchall()

        matched = None
        for r in rows:
            # Constant-time compare so timing does not reveal a partial match.
            if hmac.compare_digest(r[4] or "", token_hash):
                matched = r
                break

        if matched is None:
            # Do not say whether the token was wrong, expired, or already used.
            log_event(cur, None, "bad_token", actor, {"decision": decision})
            conn.commit()
            print("  No pending approval matches that token.")
            return 1

        (request_id, atype, application_id, _status,
         _hash, expires, attempts, max_attempts, summary) = matched

        cur.execute(
            """
            SELECT type, application_id::text, payload_json, status, summary_text
            FROM approval_requests
            WHERE id = %s;
            """,
            (request_id,),
        )
        request_row = cur.fetchone()
        if not request_row:
            conn.rollback()
            print("  Approval request disappeared.")
            return 1
        payload_request = {
            "type": request_row[0],
            "application_id": request_row[1],
            "payload_json": request_row[2] or {},
            "status": request_row[3],
            "summary_text": request_row[4],
        }
        # A positive authorization must still bind to the exact current state.
        # A denial is always safe to record even when the underlying document,
        # page, or input hash became stale; otherwise a stale capability can be
        # impossible to close until its TTL expires.
        if decision == "approve":
            try:
                assert_binding_matches(cur, payload_request)
            except RuntimeError as e:
                log_event(cur, request_id, "binding_mismatch", actor, {"error": str(e)})
                conn.commit()
                print(f"  {e}")
                return 1

        if attempts >= max_attempts:
            log_event(cur, request_id, "locked_out", actor, {})
            conn.commit()
            print(f"  Request {request_id} is locked out after {attempts} attempts.")
            return 1

        new_status = "approved" if decision == "approve" else "denied"

        cur.execute(
            """
        UPDATE approval_requests
        SET status = %s, action_taken = %s, action_note = %s,
                responded_at = now()
        WHERE id = %s AND status = 'pending';
        """,
        (new_status, decision, note, request_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print("  Request changed state concurrently. Nothing done.")
            return 1

        if atype == "fit_review":
            _resolve_fit_review_transition(
                cur, application_id=application_id, request_id=request_id,
                approved=(new_status == "approved"), actor=actor,
            )

        if new_status == "denied":
            denied_payload = payload_request["payload_json"] if isinstance(payload_request.get("payload_json"), dict) else {}
            _reject_email_candidate_for_denied_request(
                cur, application_id=application_id, atype=atype, payload=denied_payload,
            )
            _stop_application_for_denied_privileged(
                cur, application_id=application_id, atype=atype,
                payload={**denied_payload, "_request_id": request_id}, actor=actor,
            )

        autofill_queued = False
        if application_id and new_status == "denied" and atype == "autofill_form":
            parent_payload = payload_request["payload_json"] if isinstance(payload_request.get("payload_json"), dict) else {}
            _restore_autofill_ready_after_terminal_parent(
                cur, application_id=application_id,
                plan_key=str(parent_payload.get("autofill_plan_key") or ""),
                reason="Human denied the exact autofill capability before browser I/O.",
                parent_request_id=str(request_id),
            )
        if application_id and new_status == "approved" and atype == "autofill_form":
            payload = payload_request["payload_json"]
            autofill_queued = queue_ready_autofill_for_plan(
                cur, application_id=application_id,
                plan_key=str(payload.get("autofill_plan_key") or ""), actor=actor,
            )
        elif application_id and atype == "privileged_upload_document":
            child_payload = payload_request["payload_json"]
            if isinstance(child_payload, dict) and child_payload.get("delegated_to_autofill") is True:
                autofill_queued = queue_ready_autofill_for_plan(
                    cur, application_id=application_id,
                    plan_key=str(child_payload.get("autofill_plan_key") or ""), actor=actor,
                )

        log_event(cur, request_id, new_status, actor, {"note": note})
        conn.commit()

        print(f"\n  {new_status.upper()}")
        print(f"  request:     {request_id}")
        print(f"  type:        {atype}")
        print(f"  summary:     {summary}")
        if application_id and new_status == "approved" and atype == "autofill_form":
            print("\n  One document/page/input-bound autofill capability is approved.")
            if autofill_queued:
                print("  All delegated upload gates are resolved; the one-time browser task was queued.")
            else:
                print("  Waiting for separate document-upload decisions before the browser task can queue.")
            print("  It re-checks page identity and verifies every write. It never submits the application.")
        return 0


def decide_request_by_id(conn, request_id: str, *, decision: str, note: str,
                         actor: str, commit: bool = True) -> dict[str, object]:
    """Apply a Review Hub decision through the canonical approval boundary.

    The caller is an authenticated local review UI adapter, not a bearer-token
    shortcut. Exact document, page, artifact, input-hash and action-scope
    checks run again before a one-time browser task can be queued.
    """
    if decision not in {"approve", "reject", "deny"}:
        return {"ok": False, "error": "decision must be approve/reject"}
    normalized = "approve" if decision == "approve" else "deny"
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE approval_requests SET status = 'expired'
                WHERE id = %s AND status = 'pending' AND token_expires_at <= now();""",
            (request_id,),
        )
        cur.execute(
            """SELECT id::text, type, application_id::text, status, attempt_count,
                      max_attempts, payload_json, summary_text
                 FROM approval_requests WHERE id = %s FOR UPDATE;""",
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "Approval request not found."}
        rid, atype, application_id, status, attempts, max_attempts, payload, summary = row
        if status != "pending":
            return {"ok": False, "error": f"Approval request is {status}."}
        if attempts >= max_attempts:
            return {"ok": False, "error": "Approval request is locked out."}
        request = {"type": atype, "application_id": application_id,
                   "payload_json": payload or {}, "status": status,
                   "summary_text": summary}
        if normalized == "approve":
            try:
                assert_binding_matches(cur, request)
            except RuntimeError as exc:
                log_event(cur, rid, "binding_mismatch", actor, {"error": str(exc)})
                if commit:
                    conn.commit()
                return {"ok": False, "error": str(exc)}
        new_status = "approved" if normalized == "approve" else "denied"
        cur.execute(
            """UPDATE approval_requests
                  SET status = %s, action_taken = %s, action_note = %s, responded_at = now()
                WHERE id = %s AND status = 'pending';""",
            (new_status, normalized, note, rid),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "Approval request changed state concurrently."}
        if atype == "fit_review":
            _resolve_fit_review_transition(
                cur, application_id=application_id, request_id=rid,
                approved=(new_status == "approved"), actor=actor,
            )
        if new_status == "denied":
            denied_payload = request["payload_json"] if isinstance(request.get("payload_json"), dict) else {}
            _reject_email_candidate_for_denied_request(
                cur, application_id=application_id, atype=atype, payload=denied_payload,
            )
            _stop_application_for_denied_privileged(
                cur, application_id=application_id, atype=atype,
                payload={**denied_payload, "_request_id": rid}, actor=actor,
            )
        autofill_queued = False
        if application_id and new_status == "denied" and atype == "autofill_form":
            parent_payload = request["payload_json"] if isinstance(request.get("payload_json"), dict) else {}
            _restore_autofill_ready_after_terminal_parent(
                cur, application_id=application_id,
                plan_key=str(parent_payload.get("autofill_plan_key") or ""),
                reason="Human denied the exact autofill capability before browser I/O.",
                parent_request_id=str(rid),
            )
        if application_id and new_status == "approved" and atype == "autofill_form":
            bound = request["payload_json"]
            autofill_queued = queue_ready_autofill_for_plan(
                cur, application_id=application_id,
                plan_key=str(bound.get("autofill_plan_key") or ""), actor=actor,
            )
        elif application_id and atype == "privileged_upload_document":
            child = request["payload_json"]
            if isinstance(child, dict) and child.get("delegated_to_autofill") is True:
                autofill_queued = queue_ready_autofill_for_plan(
                    cur, application_id=application_id,
                    plan_key=str(child.get("autofill_plan_key") or ""), actor=actor,
                )

        log_event(cur, rid, new_status, actor, {"note": note, "channel": "trusted_review_ui"})
    if commit:
        conn.commit()
    return {"ok": True, "request_id": rid, "status": new_status,
            "type": atype, "application_id": application_id,
            "autofill_queued": bool(autofill_queued),
            "delegated_to_autofill": bool((request.get("payload_json") or {}).get("delegated_to_autofill"))}


# ---------------------------------------------------------------- list / expire

def cmd_list(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT approval_request_id::text, type, company, job_title,
                   summary_text, token_expires_at, attempt_count
            FROM v_approvals_actionable;
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("\nNo actionable approvals.")
        else:
            print(f"\n{len(rows)} pending approval(s):\n")
            for rid, atype, company, title, summary, expires, attempts in rows:
                print(f"  {rid}")
                print(f"    type:     {atype}")
                print(f"    subject:  {summary or f'{company} / {title}'}")
                print(f"    expires:  {expires}")
                if attempts:
                    print(f"    attempts: {attempts}")
                print()

        if args.show_history:
            cur.execute(
                """
                SELECT ar.id::text, ar.type, ar.status, ar.responded_at, ar.action_note
                FROM approval_requests ar
                WHERE ar.status <> 'pending'
                ORDER BY ar.responded_at DESC NULLS LAST LIMIT 20;
                """
            )
            hist = cur.fetchall()
            if hist:
                print("Recent decisions:")
                for rid, atype, status, when, note in hist:
                    print(f"  {status:9s} {atype:20s} {str(when)[:19]}  {note or ''}")
    return 0


def cmd_expire_stale(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE approval_requests
            SET status = 'expired', executing_task_id=NULL
            WHERE status IN ('pending', 'approved') AND token_expires_at <= now()
            RETURNING id::text, type, application_id::text, payload_json;
            """
        )
        expired = [(str(r[0]), str(r[1] or ""), str(r[2] or ""), dict(r[3] or {})) for r in cur.fetchall()]
        ids = [row[0] for row in expired]
        for rid, atype, application_id, payload in expired:
            log_event(cur, rid, "expired", "system", {})
            if atype == "autofill_form" and application_id:
                _restore_autofill_ready_after_terminal_parent(
                    cur, application_id=application_id,
                    plan_key=str(payload.get("autofill_plan_key") or ""),
                    reason="Autofill approval expired before browser I/O.",
                    parent_request_id=rid,
                )
        if not args.apply:
            conn.rollback()
            print(f"DRY RUN: {len(ids)} request(s) would be expired.")
            return 0
        conn.commit()
        print(f"Expired {len(ids)} request(s).")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L1 approval service")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("create")
    pc.add_argument("--type", required=True, choices=APPROVAL_TYPES)
    pc.add_argument("--application-id")
    pc.add_argument("--summary")
    pc.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
    pc.add_argument("--max-attempts", type=int, default=5)
    pc.add_argument("--channel", default="cli")
    pc.add_argument("--requested-by", default="orchestrator")
    pc.add_argument("--document-id", help="Required for type=autofill_form.")
    pc.add_argument("--artifact-id", help="Optional exact resume/cover artifact to authorize for upload.")
    pc.add_argument("--expected-origin", help="Required for type=autofill_form, e.g. https://jobs.example.com")
    pc.add_argument("--expected-target-id", help="Exact JobOS browser target id for type=autofill_form.")
    pc.add_argument("--expected-page-url", help="Exact initial application URL for type=autofill_form.")
    pc.add_argument(
        "--expected-page-fingerprint",
        help=(
            "Read-only snapshot SHA-256 page identity "
            "for type=autofill_form."
        ),
    )
    pc.add_argument(
        "--expected-autofill-input-hash",
        help=(
            "Exact input SHA-256 emitted by jobos autofill prepare; "
            "creation fails if inputs changed."
        ),
    )
    pc.add_argument("--autofill-action-scope-json", help="Exact action-scope JSON emitted by jobos autofill prepare.")
    pc.add_argument("--autofill-plan-key", help="Exact parent-plan SHA-256 linking autofill to delegated upload gates.")
    pc.add_argument("--expected-upload-capabilities-json", default="[]",
                    help="Exact delegated upload child identities that must exist before parent queueing.")
    pc.add_argument("--delegated-upload-packages-json", default="[]",
                    help="Exact child packages stored on the parent so interrupted materialization can self-repair.")
    pc.add_argument("--review-context-json", help="Best-effort human-review context. Missing context never weakens execution bindings.")
    pc.add_argument("--apply", action="store_true")

    pa = sub.add_parser("approve")
    pa.add_argument("--token", required=True)
    pa.add_argument("--note", default="")
    pa.add_argument("--actor", default="user")

    pd = sub.add_parser("deny")
    pd.add_argument("--token", required=True)
    pd.add_argument("--note", default="")
    pd.add_argument("--actor", default="user")

    pl = sub.add_parser("list")
    pl.add_argument("--show-history", action="store_true")

    pe = sub.add_parser("expire-stale")
    pe.add_argument("--apply", action="store_true")

    args = p.parse_args()

    print(f"===== APPROVAL SERVICE ({SERVICE_VERSION}) =====")

    with psycopg.connect(DSN, autocommit=False) as conn:
        if args.command == "create":
            return cmd_create(conn, args)
        if args.command == "approve":
            return redeem(conn, args.token, decision="approve",
                          note=args.note, actor=args.actor)
        if args.command == "deny":
            return redeem(conn, args.token, decision="deny",
                          note=args.note, actor=args.actor)
        if args.command == "list":
            return cmd_list(conn, args)
        if args.command == "expire-stale":
            return cmd_expire_stale(conn, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
