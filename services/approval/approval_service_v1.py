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
from services.autofill.autofill_context_v1 import load_autofill_context

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
        SELECT id::text, doc_type, version, content
        FROM generated_documents
        WHERE id = %s AND application_id = %s
          AND qa_status = 'pass' AND approved = true;
        """,
        (document_id, application_id),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(
            "document must belong to this application and have passed QA and user approval."
        )
    return {
        "id": row[0], "doc_type": row[1], "version": row[2],
        "content_hash": hashlib.sha256((row[3] or "").encode("utf-8")).hexdigest(),
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


def log_event(cur, request_id: Optional[str], event: str,
              actor: str, detail: Optional[Dict[str, Any]] = None) -> None:
    cur.execute(
        """
        INSERT INTO approval_events (approval_request_id, event, actor, detail_json)
        VALUES (%s, %s, %s, %s);
        """,
        (request_id, event, actor, Jsonb(detail or {})),
    )


# ---------------------------------------------------------------- create

def cmd_create(conn, args) -> int:
    with conn.cursor() as cur:
        if args.application_id:
            cur.execute(
                "SELECT company, job_title, current_step FROM applications WHERE id = %s;",
                (args.application_id,),
            )
            row = cur.fetchone()
            if not row:
                print(f"ERROR: application not found: {args.application_id}")
                return 1
            company, job_title, step = row
            summary = args.summary or (
                f"{args.type}: {company} / {job_title} (currently at {step})"
            )
        else:
            company = job_title = step = None
            summary = args.summary or args.type

        payload = {"company": company, "job_title": job_title,
                   "service_version": SERVICE_VERSION}
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
            if not all((args.application_id, args.document_id, args.expected_origin,
                        args.expected_page_url, args.expected_page_fingerprint, args.expected_autofill_input_hash,)):
                print("ERROR: autofill_form requires --application-id, --document-id, --expected-origin, --expected-page-url, --expected-page-fingerprint and --expected-autofill-input-hash.")
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
                if not isinstance(action_scope, dict) or not isinstance(action_scope.get("profile_keys"), list):
                    raise RuntimeError("--autofill-action-scope-json must contain profile_keys from jobos autofill prepare.")
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
            except (RuntimeError, json.JSONDecodeError) as exc:
                print(f"ERROR: {exc}")
                return 1
            payload.update({
                "document_id": binding["id"],
                "document_sha256": binding["content_hash"],
                "expected_origin": expected_origin,
                "expected_initial_url": expected_page_url,
                "expected_page_fingerprint": args.expected_page_fingerprint.casefold(),
                "autofill_input_hash": input_hash,
                "artifact_id": artifact["id"] if artifact else None,
                "artifact_sha256": artifact["sha256"] if artifact else None,
                "artifact_filename": artifact["filename"] if artifact else None,
                "autofill_action_scope": action_scope,
            })
            idempotency_key = hash_json({
                "type": args.type, "application_id": args.application_id,
                "document_id": binding["id"], "document_sha256": binding["content_hash"],
                "expected_origin": expected_origin, "expected_initial_url": expected_page_url,
                "expected_page_fingerprint": args.expected_page_fingerprint.casefold(), "autofill_input_hash": input_hash,
                "artifact_id": artifact["id"] if artifact else None,
                "artifact_sha256": artifact["sha256"] if artifact else None,
                "autofill_action_scope": action_scope,
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
        request_id, expires = cur.fetchone()
        log_event(cur, request_id, "created", args.requested_by,
                  {"type": args.type, "ttl_minutes": args.ttl_minutes})

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

        if application_id and new_status == "approved" and atype == "autofill_form":
            payload = payload_request["payload_json"]
            cur.execute(
                """
                INSERT INTO browser_tasks
                  (task_type, requested_by, application_id, status, priority,
                   input_json, approval_request_id, expected_origin,
                   generated_document_id, document_sha256, timeout_seconds,
                   bound_artifact_id, artifact_sha256, artifact_filename,
                   expected_initial_url, expected_page_fingerprint, autofill_input_hash,
                   autofill_action_scope,
                   idempotency_key, created_at)
                VALUES ('fill_application_form', %s, %s, 'queued', 'high',
                        '{}'::jsonb, %s, %s, %s, %s, 300, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (approval_request_id) WHERE approval_request_id IS NOT NULL DO NOTHING;
                """,
                (actor, application_id, request_id, payload["expected_origin"],
                 payload["document_id"], payload["document_sha256"], payload.get("artifact_id"),
                 payload.get("artifact_sha256"), payload.get("artifact_filename"), payload["expected_initial_url"],
                 payload["expected_page_fingerprint"], payload["autofill_input_hash"],
                 Jsonb(payload.get("autofill_action_scope") or {}), f"autofill:{request_id}"),
            )
            log_event(cur, request_id, "autofill_task_queued", actor, {
                "expected_origin": payload["expected_origin"], "document_id": payload["document_id"],
            })

        log_event(cur, request_id, new_status, actor, {"note": note})
        conn.commit()

        print(f"\n  {new_status.upper()}")
        print(f"  request:     {request_id}")
        print(f"  type:        {atype}")
        print(f"  summary:     {summary}")
        if application_id and new_status == "approved" and atype == "autofill_form":
            print("\n  One document/page/input-bound autofill capability is approved.")
            print("  A deterministic one-time browser task was queued; it re-checks page identity and")
            print("  verifies every write. It never submits the application.")
        return 0


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
            SET status = 'expired'
            WHERE status IN ('pending', 'approved') AND token_expires_at <= now()
            RETURNING id::text;
            """
        )
        ids = [r[0] for r in cur.fetchall()]
        for rid in ids:
            log_event(cur, rid, "expired", "system", {})
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
    pc.add_argument("--expected-page-url", help="Exact initial application URL for type=autofill_form.")
    pc.add_argument(
        "--expected-autofill-input-hash",
        help=(
            "Exact input SHA-256 emitted by jobos autofill prepare; "
            "creation fails if inputs changed."
        ),
    )
    pc.add_argument("--autofill-action-scope-json", help="Exact action-scope JSON emitted by jobos autofill prepare.")
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
