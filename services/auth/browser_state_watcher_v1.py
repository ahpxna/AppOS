"""Read-only browser-state watcher for one-click auth/checkpoint continuation.

The watcher never clicks, types, uploads, or submits.  It only re-observes the
exact target already stored in application_auth_sessions.  When the human has
completed login/MFA/CAPTCHA manually and the exact page now classifies as a
new authoritative state, JobOS records that observation and idempotently
materializes the next human gate.
"""
from __future__ import annotations

import argparse
import re
import time
from typing import Any

from services.application_actions.privileged_action_v1 import (
    _host_is_allowed,
    _post_commit_followup,
    _snapshot,
    _transport,
    _update_auth_session,
    canonical_pipeline_step_for_browser_state,
    canonical_page_url,
    detect_page_state,
    detect_platform,
)

WATCH_STEPS = {
    "needs_account_auth",
    "needs_mfa",
    "needs_human_checkpoint",
}


def _identity_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _application_form_identity_is_grounded(*, live_url: str, snapshot: dict[str, Any],
                                             job_url: str, company: str, job_title: str) -> bool:
    """Fail closed before treating a manually redirected tab as this app's form.

    Login/MFA redirects legitimately lose the original URL. Once the page is
    classified as an application form, however, at least the exact application
    URL or the durable role identity must be observable. A same-domain form for
    another open application must never advance this application.
    """
    try:
        if job_url and canonical_page_url(live_url) == canonical_page_url(job_url):
            return True
    except Exception:
        pass
    haystack = _identity_text(str(snapshot.get("snapshot") or ""))
    title = _identity_text(job_title)
    company_text = _identity_text(company)
    if not title or len(title) < 4:
        return False
    title_match = title in haystack
    # Company is secondary evidence because white-label ATS pages sometimes
    # omit it; when visible it must agree rather than weakening the title bind.
    company_match = not company_text or company_text in haystack
    return bool(title_match and company_match)


def observe_once(conn, *, limit: int = 20) -> list[dict[str, Any]]:
    """Observe exact bound auth targets; return only applications that changed."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id::text,a.current_step,s.current_url,s.page_fingerprint,s.auth_state,s.detail_json,
                      coalesce(a.job_url,''),coalesce(a.jd_hash,''),coalesce(a.company,''),coalesce(a.job_title,''),
                      coalesce(s.binding_job_url,''),coalesce(s.binding_jd_hash,'')
                 FROM applications a
                 JOIN application_auth_sessions s ON s.application_id=a.id
                WHERE a.current_step = ANY(%s)
                  AND a.status NOT IN ('submitted','abandoned')
                ORDER BY s.updated_at
                LIMIT %s;""",
            (list(WATCH_STEPS), max(1, min(int(limit), 100))),
        )
        seeds = [
            (str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or ""), str(r[4] or ""), dict(r[5] or {}),
             str(r[6] or ""),str(r[7] or ""),str(r[8] or ""),str(r[9] or ""),str(r[10] or ""),str(r[11] or ""))
            for r in cur.fetchall()
        ]

    if not seeds:
        return []
    transport = _transport()
    changed: list[dict[str, Any]] = []
    for (application_id, expected_step, old_url, old_fp, old_auth_state, detail,
         job_url, jd_hash, company, job_title, bound_job_url, bound_jd_hash) in seeds:
        if bound_job_url != job_url or bound_jd_hash != jd_hash:
            # Application/JD identity changed after this browser session was
            # established. Never let the old tab become authority for new work.
            continue
        target_id = str(detail.get("target_id") or "")
        if not target_id:
            continue
        try:
            live_url, snap, nodes, fp = _snapshot(transport, target_id)
        except Exception:
            continue
        live_state, live_detail = detect_page_state(live_url, snap, nodes)
        if live_state in {"application_form_ready", "authenticated"} and not _application_form_identity_is_grounded(
            live_url=live_url, snapshot=snap, job_url=job_url,
            company=company, job_title=job_title,
        ):
            # Exact target and trusted origin are insufficient when a human can
            # navigate the same tab to another job on the same ATS.
            continue
        platform = detect_platform(live_url, snap)
        with conn.cursor() as cur:
            # Trust is rechecked before using browser evidence.  The watcher is
            # read-only, but untrusted pages must not mutate authoritative state.
            # A legitimate same-tab SSO redirect still needs a recoverable
            # trust decision instead of becoming an invisible dead-end.
            if not _host_is_allowed(cur, live_url, application_id=application_id, purpose="employer_handoff"):
                cur.execute(
                    """SELECT 1 FROM approval_requests
                        WHERE application_id=%s AND type='privileged_trust_external_domain'
                          AND status IN ('pending','approved','executing')
                          AND (status='executing' OR token_expires_at>now())
                          AND payload_json->>'target_id'=%s
                          AND payload_json->>'expected_url'=%s
                        LIMIT 1;""",
                    (application_id, target_id, canonical_page_url(live_url)),
                )
                trust_already_actionable = cur.fetchone() is not None
                conn.rollback()
                if trust_already_actionable:
                    continue
                result = {
                    "target_id": target_id, "url": live_url,
                    "page_fingerprint": fp, "state": live_state,
                    "platform": platform, "followup": "trust_domain_required",
                }
                try:
                    followup = _post_commit_followup(conn, application_id, result)
                except Exception as exc:
                    followup = {"ok": False, "error": str(exc)[:1000]}
                changed.append({
                    "application_id": application_id, "from_step": expected_step,
                    "state": live_state, "url": live_url, "followup": followup,
                    "trust_required": True,
                })
                continue
            canonical_step = canonical_pipeline_step_for_browser_state(live_state)
            if not canonical_step:
                conn.rollback()
                continue
            if (canonical_step == expected_step and live_url == old_url and fp == old_fp
                    and live_state == old_auth_state):
                # No new semantic event and no redirect/fingerprint update:
                # remain genuinely read-only instead of rewriting an auth
                # session on every polling tick.
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
            # Keep the exact target binding fresh through legitimate same-tab
            # login redirects even when the canonical application state has
            # not changed (manual SSO and account auth are the same durable
            # state).  It is an observation only, not a repeated follow-up.
            _update_auth_session(
                cur, application_id=application_id, url=live_url, fingerprint=fp,
                state=live_state, platform=platform,
                detail={**live_detail, "target_id": target_id, "observed_by": "browser_state_watcher_v1"}, snapshot=snap,
            )
        conn.commit()
        if canonical_step == expected_step:
            continue
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
