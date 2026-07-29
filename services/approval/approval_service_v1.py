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

  Redemption is single-use and constant-time compared. Wrong tokens increment
  attempt_count; after max_attempts the request locks itself out, so a token
  cannot be brute-forced even if the request id leaks.

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
import os
import secrets
import sys
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

SERVICE_VERSION = "approval_service_v1_single_use_2026_07_29"

APPROVAL_TYPES = (
    "submit_application",   # L7: fill a real application form
    "send_message",         # L8: send a reply to a recruiter
    "spend_over_budget",    # L1: exceed the daily cost budget
    "browser_login",        # L3: open a session on a site requiring login
)

DEFAULT_TTL_MINUTES = 60


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

        # Only issue an approval when there is something concrete to approve.
        if args.type == "submit_application" and args.application_id:
            cur.execute(
                """
                SELECT count(*) FROM generated_documents
                WHERE application_id = %s AND qa_status = 'pass';
                """,
                (args.application_id,),
            )
            if cur.fetchone()[0] == 0:
                print(
                    "ERROR: no document has passed the truth checker for this "
                    "application. Approving a submission with nothing verified "
                    "to submit would defeat the gate."
                )
                return 1

        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)

        cur.execute(
            """
            INSERT INTO approval_requests
              (type, application_id, payload_json, status, approval_channel,
               approval_token_hash, token_expires_at, requested_by, summary_text,
               max_attempts, created_at)
            VALUES (%s, %s, %s, 'pending', %s, %s,
                    now() + make_interval(mins => %s), %s, %s, %s, now())
            RETURNING id::text, token_expires_at;
            """,
            (
                args.type, args.application_id,
                Jsonb({"company": company, "job_title": job_title,
                       "service_version": SERVICE_VERSION}),
                args.channel, token_hash, args.ttl_minutes,
                args.requested_by, summary, args.max_attempts,
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
            WHERE status = 'pending' AND token_expires_at <= now();
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
            cur.execute(
                """
                UPDATE approval_requests
                SET attempt_count = attempt_count + 1
                WHERE status = 'pending' AND token_expires_at > now();
                """
            )
            conn.commit()
            print("  No pending approval matches that token.")
            return 1

        (request_id, atype, application_id, _status,
         _hash, expires, attempts, max_attempts, summary) = matched

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
                responded_at = now(), consumed_at = now(), consumed_by = %s
            WHERE id = %s AND status = 'pending';
            """,
            (new_status, decision, note, actor, request_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print("  Request changed state concurrently. Nothing done.")
            return 1

        log_event(cur, request_id, new_status, actor, {"note": note})
        conn.commit()

        print(f"\n  {new_status.upper()}")
        print(f"  request:     {request_id}")
        print(f"  type:        {atype}")
        print(f"  summary:     {summary}")
        if application_id and new_status == "approved" and atype == "submit_application":
            print("\n  L7 may now queue a fill_application_form task for "
                  f"application {application_id}.")
            print("  Note: filling a form is still not submitting it. "
                  "The final submit remains a human action.")
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
            WHERE status = 'pending' AND token_expires_at <= now()
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
