#!/usr/bin/env python3
"""Run one bounded recruiter-message classify/draft/QA control-plane cycle.

Inbound transport remains provider-specific and separate. Once a provider or
operator has durably inserted ``messages``, this worker owns the deterministic
handoff through classification, grounded draft, truth QA, and Review Hub
materialization. It never sends an external message.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.config import database_dsn, env_int, load_repo_env
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER


def _run(argv: list[str], *, timeout: int) -> tuple[bool, str]:
    result = DEFAULT_PROCESS_RUNNER.run(argv, cwd=ROOT, timeout_s=timeout)
    detail = (result.output or result.start_error or "").strip()
    return result.ok, detail[-2000:]


def run_once(*, limit: int = 20, timeout: int = 900) -> int:
    import psycopg

    load_repo_env()
    bounded_limit = max(1, min(int(limit), 100))
    script = str(ROOT / "services" / "messaging" / "message_reply_v1.py")

    ok, detail = _run(
        [sys.executable, script, "classify", "--pending", "--limit", str(bounded_limit), "--apply"],
        timeout=timeout,
    )
    if not ok:
        print(f"message classification failed: {detail}", file=sys.stderr)
        return 1

    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT v.thread_id::text
                 FROM v_threads_needing_reply v
                 JOIN message_threads mt ON mt.id=v.thread_id
                WHERE coalesce(v.needs_human,false)=false
                  AND coalesce(mt.needs_user_attention,false)=false
                  AND v.linked_application_id IS NOT NULL
                  AND v.unprocessed_inbound=0
                ORDER BY v.last_message_at NULLS FIRST, v.thread_id
                LIMIT %s;""",
            (bounded_limit,),
        )
        thread_ids = [str(row[0]) for row in cur.fetchall()]

    failures: list[str] = []
    for thread_id in thread_ids:
        ok, detail = _run(
            [sys.executable, script, "draft", "--thread-id", thread_id, "--apply"],
            timeout=timeout,
        )
        if not ok:
            failures.append(f"draft {thread_id}: {detail}")

    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT dr.id::text,dr.thread_id::text
                 FROM drafted_replies dr
                WHERE dr.qa_status IN ('revise','fail') AND dr.sent=false
                  AND dr.superseded_at IS NULL
                  AND dr.revision_round < 2
                  AND NOT EXISTS (SELECT 1 FROM drafted_replies child WHERE child.revision_of=dr.id)
                ORDER BY dr.created_at
                LIMIT %s;""",
            (bounded_limit,),
        )
        revisions = [(str(row[0]), str(row[1])) for row in cur.fetchall()]
    for reply_id, thread_id in revisions:
        ok, detail = _run(
            [sys.executable, script, "draft", "--thread-id", thread_id,
             "--revision-of", reply_id, "--apply"],
            timeout=timeout,
        )
        if not ok:
            failures.append(f"revise {reply_id}: {detail}")

    ok, detail = _run(
        [sys.executable, script, "verify", "--pending", "--apply"], timeout=timeout,
    )
    if not ok:
        failures.append(f"truth QA: {detail}")

    if failures:
        print("\n".join(failures)[-6000:], file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=env_int(
        "JOBOS_MESSAGE_WORKER_BATCH_SIZE", 20, minimum=1, maximum=100
    ))
    parser.add_argument("--timeout", type=int, default=env_int(
        "JOBOS_MESSAGE_WORKER_TIMEOUT_SECONDS", 900, minimum=60, maximum=3600
    ))
    args = parser.parse_args()
    return run_once(limit=args.limit, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
