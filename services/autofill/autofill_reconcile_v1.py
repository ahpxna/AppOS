#!/usr/bin/env python3
"""Inspect and explicitly close a non-replayable deterministic autofill task."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
import psycopg
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn

def show(cur, task_id: str) -> None:
    cur.execute(
        """SELECT b.id::text, b.execution_state, b.status, b.pinned_target_id,
                  b.error_message, a.id::text, a.status
           FROM browser_tasks b JOIN approval_requests a ON a.id = b.approval_request_id
           WHERE b.id = %s""",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("Task not found.")
    print({"task_id": row[0], "execution_state": row[1], "task_status": row[2],
           "pinned_target_id": row[3], "error": row[4], "approval_id": row[5],
           "approval_status": row[6]})
    cur.execute(
        """SELECT sequence_no, action_kind, target_ref, status, started_at, verified_at
           FROM autofill_action_journal WHERE browser_task_id = %s ORDER BY sequence_no""",
        (task_id,),
    )
    for item in cur.fetchall():
        print({"sequence": item[0], "action": item[1], "target": item[2],
               "status": item[3], "started_at": str(item[4]), "verified_at": str(item[5])})

def close(cur, task_id: str) -> None:
    cur.execute(
        """UPDATE approval_requests a SET status = 'expired', executing_task_id = NULL
           FROM browser_tasks b
           WHERE b.id = %s AND a.id = b.approval_request_id
             AND a.status = 'executing' AND b.execution_state = 'needs_reconciliation'""",
        (task_id,),
    )
    if cur.rowcount != 1:
        raise SystemExit("Only an executing task marked needs_reconciliation can be closed.")
    cur.execute("UPDATE browser_tasks SET status = 'failed', finished_at = now() WHERE id = %s", (task_id,))
    print("Closed non-replayable capability. Inspect the form, then issue a fresh approval if needed.")

def main() -> int:
    p = argparse.ArgumentParser(description="Review or close a non-replayable JobOS autofill task.")
    p.add_argument("task_id")
    p.add_argument("--close", action="store_true")
    args = p.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        show(cur, args.task_id)
        if args.close:
            close(cur, args.task_id)
            conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
