"""Read-only browser-state watcher for one-click auth/checkpoint continuation.

The watcher never clicks, types, uploads, or submits.  It only re-observes the
exact target already stored in application_auth_sessions.  When the human has
completed login/MFA/CAPTCHA manually and the exact page now classifies as a
new authoritative state, JobOS records that observation and idempotently
materializes the next human gate.
"""
from __future__ import annotations

import argparse
import time
from typing import Any

from services.application_actions.privileged_action_v1 import (
    _host_is_allowed,
    _post_commit_followup,
    _snapshot,
    _transport,
    _update_auth_session,
    detect_page_state,
    detect_platform,
)

WATCH_STEPS = {
    "needs_account_auth",
    "needs_mfa",
    "needs_human_checkpoint",
}


def observe_once(conn, *, limit: int = 20) -> list[dict[str, Any]]:
    """Observe exact bound auth targets; return only applications that changed."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id::text,a.current_step,s.current_url,s.page_fingerprint,s.detail_json
                 FROM applications a
                 JOIN application_auth_sessions s ON s.application_id=a.id
                WHERE a.current_step = ANY(%s)
                  AND a.status NOT IN ('submitted','abandoned')
                ORDER BY s.updated_at
                LIMIT %s;""",
            (list(WATCH_STEPS), max(1, min(int(limit), 100))),
        )
        seeds = [(str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or ""), dict(r[4] or {})) for r in cur.fetchall()]

    if not seeds:
        return []
    transport = _transport()
    changed: list[dict[str, Any]] = []
    for application_id, expected_step, old_url, _old_fp, detail in seeds:
        target_id = str(detail.get("target_id") or "")
        if not target_id:
            continue
        try:
            live_url, snap, nodes, fp = _snapshot(transport, target_id)
        except Exception:
            continue
        with conn.cursor() as cur:
            # Trust is rechecked before using browser evidence.  The watcher is
            # read-only, but untrusted pages must not mutate authoritative state.
            if not _host_is_allowed(cur, live_url, application_id=application_id, purpose="employer_handoff"):
                conn.rollback()
                continue
            live_state, live_detail = detect_page_state(live_url, snap, nodes)
            if live_state not in {
                "application_form_ready", "authenticated", "needs_account_auth",
                "needs_email_verification", "needs_mfa", "needs_human_checkpoint",
                "needs_manual_sso",
            }:
                conn.rollback()
                continue
            if live_state == expected_step:
                conn.rollback()
                continue
            cur.execute(
                """SELECT a.current_step,s.detail_json
                     FROM applications a JOIN application_auth_sessions s ON s.application_id=a.id
                    WHERE a.id=%s FOR UPDATE;""", (application_id,)
            )
            current = cur.fetchone()
            if not current or str(current[0]) != expected_step:
                conn.rollback()
                continue
            current_target = str((current[1] or {}).get("target_id") or "")
            if current_target != target_id:
                conn.rollback()
                continue
            platform = detect_platform(live_url, snap)
            _update_auth_session(
                cur, application_id=application_id, url=live_url, fingerprint=fp,
                state=live_state, platform=platform,
                detail={**live_detail, "target_id": target_id, "observed_by": "browser_state_watcher_v1"},
            )
        conn.commit()
        result = {"target_id": target_id, "url": live_url, "page_fingerprint": fp,
                  "state": live_state, "platform": platform, "followup": "state_observed"}
        try:
            followup = _post_commit_followup(conn, application_id, result)
        except Exception as exc:
            followup = {"ok": False, "error": str(exc)[:1000]}
        changed.append({"application_id": application_id, "from_step": expected_step,
                        "state": live_state, "url": live_url, "followup": followup})
    return changed


def main() -> int:
    """Run observation independently from Telegram long-poll cadence."""
    parser = argparse.ArgumentParser(description="Read-only JobOS auth/browser state watcher")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from services.common.config import database_dsn, load_repo_env
    import psycopg

    load_repo_env()
    interval = max(1, min(int(args.poll_seconds), 60))
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        while True:
            try:
                changed = observe_once(conn)
                if changed:
                    print(f"Observed {len(changed)} auth/checkpoint transition(s).")
            except Exception as exc:
                conn.rollback()
                print(f"Browser state watcher soft-fail: {exc}")
            if args.once:
                return 0
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
