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
    # Lock the task/capability pair before changing either side.  Most uncertain
    # writes still have an executing approval, but a crash after
    # durable_finish_execution() can leave the approval already consumed while
    # later queue bookkeeping marks the task needs_reconciliation.  Consumed is
    # terminal and must stay consumed; closing reconciliation only retires the
    # uncertain task state and never makes that capability replayable.
    cur.execute(
        """SELECT b.approval_request_id::text, b.execution_state, a.status
             FROM browser_tasks b
             JOIN approval_requests a ON a.id = b.approval_request_id
            WHERE b.id = %s
            FOR UPDATE OF b, a""",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("Task or bound approval not found.")
    approval_id, execution_state, approval_status = row
    if execution_state != "needs_reconciliation":
        raise SystemExit("Only a task marked needs_reconciliation can be closed.")

    if approval_status == "executing":
        cur.execute(
            """UPDATE approval_requests
                  SET status = 'expired', executing_task_id = NULL,
                      action_note = COALESCE(action_note,
                          'Reconciliation closed explicitly; capability will not replay.')
                WHERE id = %s AND status = 'executing'
                RETURNING id""",
            (approval_id,),
        )
        if cur.fetchone() is None:
            raise SystemExit("Approval changed before reconciliation could be closed.")
    elif approval_status == "consumed":
        # Preserve consumed_at/consumed_by and the single-use audit record.
        cur.execute(
            """UPDATE approval_requests
                  SET executing_task_id = NULL,
                      action_note = COALESCE(action_note,
                          'Reconciliation closed after durable execution; consumed capability remains terminal.')
                WHERE id = %s AND status = 'consumed'""",
            (approval_id,),
        )
    else:
        raise SystemExit(
            f"Bound approval is {approval_status!r}; expected executing or consumed for reconciliation."
        )

    cur.execute(
        """UPDATE browser_tasks
              SET status = 'failed', execution_state = 'partial',
                  locked_by = NULL, lease_expires_at = NULL,
                  finished_at = COALESCE(finished_at, now()),
                  error_message = COALESCE(error_message, '') ||
                      E'\nReconciliation closed explicitly; capability will not replay.'
            WHERE id = %s AND execution_state = 'needs_reconciliation'
            RETURNING id;""",
        (task_id,),
    )
    if cur.fetchone() is None:
        raise SystemExit("Underlying browser task changed before reconciliation could be closed.")
    cur.execute(
        """UPDATE application_attempts
              SET status = 'reconciled', finished_at = COALESCE(finished_at, now()),
                  detail_json = detail_json || '{"reconciled": true, "replay": false}'::jsonb
            WHERE browser_task_id = %s AND status IN ('needs_review', 'completed', 'partial');""",
        (task_id,),
    )
    # Reconciliation is the explicit human fence that retires the uncertain
    # execution.  Only after that fence may the application return to a
    # recoverable form-review state.  The old approval remains terminal and a
    # fresh approval/page binding is required for any later write.
    cur.execute(
        """SELECT application_id::text FROM browser_tasks WHERE id = %s""",
        (task_id,),
    )
    app_row = cur.fetchone()
    if app_row and app_row[0]:
        application_id = app_row[0]
        cur.execute(
            """UPDATE applications
                  SET current_step = 'application_form_ready', updated_at = now()
                WHERE id = %s AND current_step = 'autofill_executing'
                RETURNING id""",
            (application_id,),
        )
        if cur.fetchone() is not None:
            cur.execute(
                """INSERT INTO pipeline_events(
                           application_id, from_step, to_step, actor, reason, detail_json)
                   VALUES (%s, 'autofill_executing', 'application_form_ready',
                           'autofill-reconciliation',
                           'Human closed uncertain autofill reconciliation; a fresh approval is required.',
                           '{"replay": false, "fresh_approval_required": true}'::jsonb)""",
                (application_id,),
            )
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
