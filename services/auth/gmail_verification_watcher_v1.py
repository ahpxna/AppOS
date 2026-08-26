#!/usr/bin/env python3
"""Watch only applications currently waiting for employer email verification.

This is not an inbox crawler. Each iteration searches a bounded window for the
exact candidate email and explicitly checks Spam. It stops touching an
application as soon as a verification candidate/approval exists.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.application_actions.action_request_v1 import create_privileged_request
from services.application_actions.privileged_action_v1 import VERIFY_LABELS, _find_exact_control, _find_input, _host_is_allowed, _snapshot, _transport
from services.auth.gmail_verification_v1 import discover_verification, gmail_account, persist_candidate
from services.common.config import database_dsn


def process_pending(conn, *, max_results: int = 10) -> int:
    created = 0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.application_id::text, s.account_email, s.employer_origin, s.current_url,
                      s.page_fingerprint, s.detail_json, s.updated_at
                 FROM application_auth_sessions s
                WHERE s.auth_state='needs_email_verification'
                  AND s.account_email IS NOT NULL AND btrim(s.account_email)<>''
                  AND NOT EXISTS (
                    SELECT 1 FROM approval_requests ar
                     WHERE ar.application_id=s.application_id
                       AND ar.type='privileged_use_email_verification'
                       AND ar.status IN ('pending','approved') AND ar.token_expires_at>now())
                ORDER BY s.updated_at LIMIT 20;"""
        )
        rows = cur.fetchall()
    # Release the read transaction before bounded Gmail/network calls so an idle
    # mailbox can never leave PostgreSQL idle-in-transaction while we sleep.
    conn.commit()
    for app_id, recipient, origin, current_url, stored_fp, detail, updated_at in rows:
        requested_at = updated_at - timedelta(minutes=2)
        candidate = discover_verification(recipient=recipient, requested_at=requested_at,
                                          employer_origin=origin, max_results=max_results)
        if not candidate:
            continue
        with conn.cursor() as cur:
            candidate_id = persist_candidate(cur, application_id=app_id, candidate=candidate)
            target_id = str((detail or {}).get("target_id") or "")
            field_ref = control_ref = control_label = "NaN"
            expected_url, expected_fp = current_url, stored_fp
            if target_id:
                try:
                    transport = _transport()
                    live_url, _snap, nodes, live_fp = _snapshot(transport, target_id)
                    expected_url, expected_fp = live_url, live_fp
                    if candidate["kind"] == "numeric_code":
                        field = _find_input(nodes, ("code", "verification", "otp", "one-time"))
                        if field:
                            field_ref = str(field.get("ref"))
                        try:
                            control = _find_exact_control(nodes, VERIFY_LABELS)
                            control_ref, control_label = control["ref"], control["label"]
                        except Exception:
                            pass
                except Exception:
                    pass
            if candidate["kind"] == "numeric_code" and (not field_ref or field_ref == "NaN"):
                # We found a secret candidate, but browser binding is not executable.
                # Persist hash/metadata only and route a safe read-only refocus gate
                # instead of sending Telegram an approval that must fail at execution.
                cur.execute(
                    """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                       VALUES (%s,'email_verification_binding_required','gmail-verification-watcher',%s);""",
                    (app_id, Jsonb({
                        "candidate_id": candidate_id, "gmail_message_id": candidate["message_id"],
                        "verification_kind": candidate["kind"], "secret_sha256": candidate["secret_sha256"],
                        "target_id": target_id or "NaN", "reason": "verification input could not be exact-bound",
                    })),
                )
                if target_id and expected_url and expected_fp:
                    create_privileged_request(
                        cur, application_id=app_id, action_type="privileged_auth_manual_retry",
                        payload={"target_id": target_id, "expected_url": expected_url,
                                 "expected_page_fingerprint": expected_fp, "expected_origin": origin or "NaN",
                                 "review_context": {"screenshot_path": "NaN",
                                                    "notice": "OTP found, but input binding is unavailable. Refocus the verification page, then run a fresh snapshot."}},
                        summary="OTP was found but the code field could not be exact-bound. Refocus the employer verification page, then approve AUTH RETRY; the OTP remains hash-only.",
                        requested_by="gmail-verification-watcher",
                    )
                    created += 1
                conn.commit()
                continue

            if candidate["kind"] == "magic_link":
                link_origin = str((candidate.get("secret_context") or {}).get("link_origin") or "")
                if link_origin and not _host_is_allowed(cur, link_origin, application_id=app_id, purpose="gmail_magic_link"):
                    from urllib.parse import urlsplit
                    link_host = (urlsplit(link_origin).hostname or "").casefold()
                    if link_host:
                        create_privileged_request(
                            cur, application_id=app_id, action_type="privileged_trust_external_domain",
                            payload={
                                "domain": link_host, "expected_origin": link_origin,
                                "trust_source": "gmail_magic_link", "candidate_id": candidate_id,
                                "gmail_account": gmail_account(), "gmail_message_id": candidate["message_id"],
                                "verification_kind": candidate["kind"],
                                "secret_sha256": candidate["secret_sha256"],
                                "secret_context": candidate.get("secret_context") or {},
                                "review_context": {"screenshot_path": "NaN"},
                            },
                            summary=f"Trust email-verification link domain {link_host} before JobOS opens the exact approved magic link.",
                            requested_by="gmail-verification-watcher",
                        )
                        conn.commit()
                        created += 1
                        continue

            payload = {
                "candidate_id": candidate_id, "gmail_account": gmail_account(), "gmail_message_id": candidate["message_id"],
                "sender": candidate.get("sender") or "NaN", "subject": candidate.get("subject") or "NaN",
                "received_at": candidate.get("received_at").isoformat() if candidate.get("received_at") else "NaN",
                "verification_kind": candidate["kind"], "secret_sha256": candidate["secret_sha256"],
                "secret_context": candidate.get("secret_context") or {},
                "target_id": target_id or "NaN", "expected_url": expected_url or "NaN",
                "expected_page_fingerprint": expected_fp or "NaN", "expected_origin": origin or "NaN",
                "field_ref": field_ref, "control_ref": control_ref, "control_label": control_label,
            }
            create_privileged_request(
                cur, application_id=app_id, action_type="privileged_use_email_verification", payload=payload,
                summary=f"Employer email verification found ({candidate['kind']}); secret is not stored or sent to Telegram.",
                requested_by="gmail-verification-watcher",
            )
        conn.commit()
        created += 1
    return created


def _wake_token() -> str:
    return (os.getenv("JOBOS_GMAIL_WAKE_TOKEN") or "").strip()


def _process_once(*, max_results: int) -> int:
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        return process_pending(conn, max_results=max_results)


def serve_wake_listener(*, host: str, port: int, interval_seconds: int, max_results: int) -> None:
    """Receive minimal OpenClaw wake signals and keep bounded polling fallback.

    The incoming webhook body is intentionally not trusted as verification
    evidence. A valid wake only triggers ``process_pending()``, which re-reads
    Gmail through ``gog --readonly`` using the application-scoped recipient and
    timestamp window. Spam is still checked by that bounded API query.
    """
    token = _wake_token()
    if not token:
        raise RuntimeError("JOBOS_GMAIL_WAKE_TOKEN is required for --wake-listen")
    lock = threading.Lock()

    def run_scan() -> int:
        with lock:
            return _process_once(max_results=max_results)

    class Handler(BaseHTTPRequestHandler):
        server_version = "JobOSGmailWake/1"

        def log_message(self, format, *args):
            return

        def do_POST(self):
            if self.path.rstrip("/") != "/gmail":
                self.send_error(404)
                return
            supplied = self.headers.get("x-jobos-wake-token", "")
            if not hmac.compare_digest(supplied, token):
                self.send_error(403)
                return
            try:
                length = min(max(int(self.headers.get("content-length", "0") or 0), 0), 65536)
            except ValueError:
                length = 0
            if length:
                self.rfile.read(length)  # ignored by design: hook payload is untrusted wake data
            try:
                created = run_scan()
                payload = json.dumps({"ok": True, "verification_approvals_created": created}).encode()
                self.send_response(200)
            except Exception as exc:
                payload = json.dumps({"ok": False, "error": type(exc).__name__}).encode()
                self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def fallback_loop() -> None:
        while True:
            time.sleep(max(5, interval_seconds))
            try:
                run_scan()
            except Exception:
                # Wake/poll acceleration must not crash the listener. Doctor/logs
                # expose missing gog/DB configuration to the operator.
                pass

    threading.Thread(target=fallback_loop, name="jobos-gmail-fallback", daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"gmail_wake_listener": f"http://{host}:{port}/gmail",
                      "fallback_interval_seconds": max(5, interval_seconds),
                      "spam_search": True, "hook_payload_trusted": False}))
    server.serve_forever(poll_interval=0.5)


def main() -> int:
    p = argparse.ArgumentParser(description="Bounded JobOS Gmail verification watcher")
    p.add_argument("--once", action="store_true")
    p.add_argument("--wake-listen", action="store_true", help="Listen for additive OpenClaw Gmail wake signals on loopback.")
    p.add_argument("--wake-host", default="127.0.0.1")
    p.add_argument("--wake-port", type=int, default=8791)
    p.add_argument("--interval-seconds", type=int, default=10)
    p.add_argument("--max-results", type=int, default=10)
    args = p.parse_args()
    bounded = max(1, min(args.max_results, 20))
    if args.wake_listen:
        if args.once:
            raise SystemExit("--once and --wake-listen are mutually exclusive")
        serve_wake_listener(host=args.wake_host, port=args.wake_port,
                            interval_seconds=args.interval_seconds, max_results=bounded)
        return 0
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        while True:
            count = process_pending(conn, max_results=bounded)
            if count:
                print(json.dumps({"verification_approvals_created": count}))
            if args.once:
                return 0
            time.sleep(max(5, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
