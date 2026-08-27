import socket
import uuid
import sys
from pathlib import Path
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn

WATCHDOG_ID = f"watchdog-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

def mark_uncertain_expired_tasks(conn):
    """Fail closed after any state that may have touched the browser.

    The watchdog has no browser/session context with which to prove that an
    executing/partial/completed task is safe to replay.  It therefore never
    requeues those states.  The worker's own lease reaper has the narrower
    proof for the pre-I/O executing/no-journal case.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH uncertain AS (
              UPDATE browser_tasks
              SET
                status = 'failed',
                execution_state = 'needs_reconciliation',
                locked_by = NULL,
                lease_expires_at = NULL,
                finished_at = COALESCE(finished_at, now()),
                error_message = COALESCE(error_message, '') ||
                  E'\nLease expired after browser execution may have started; manual reconciliation required.'
              WHERE
                status = 'running'
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at < now()
                AND execution_state IN ('executing', 'partial', 'completed', 'needs_reconciliation')
              RETURNING id, task_type, execution_state
            ), marked_attempts AS (
              UPDATE application_attempts a
                 SET status = 'needs_review', finished_at = COALESCE(finished_at, now()),
                     detail_json = detail_json || '{"reason":"watchdog expired after browser execution may have started"}'::jsonb
                FROM uncertain u
               WHERE a.browser_task_id = u.id AND a.status = 'started'
              RETURNING a.id
            )
            SELECT id, task_type, execution_state FROM uncertain;
            """
        )
        return cur.fetchall()


def requeue_expired_tasks(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE browser_tasks
            SET
              status = 'queued',
              retry_count = retry_count + 1,
              locked_by = NULL,
              lease_expires_at = NULL,
              error_message = COALESCE(error_message, '') || E'\nLease expired; requeued by watchdog.'
            WHERE
              status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < now()
              AND retry_count < max_retries
              AND execution_state = 'not_started'
            RETURNING id, task_type, retry_count, max_retries;
            """
        )
        return cur.fetchall()


def dead_letter_expired_tasks(conn):
    with conn.cursor() as cur:
        # A no-write task that has exhausted retries must not leave a live
        # approval capability behind, otherwise the active idempotency key can
        # prevent the user from issuing a fresh approval.
        cur.execute(
            """
            UPDATE approval_requests a
            SET status = 'expired', executing_task_id = NULL,
                action_note = COALESCE(action_note, 'Browser task exhausted before external I/O.')
            FROM browser_tasks b
            WHERE b.status = 'running'
              AND b.lease_expires_at IS NOT NULL
              AND b.lease_expires_at < now()
              AND b.retry_count >= b.max_retries
              AND b.execution_state = 'not_started'
              AND a.id = b.approval_request_id
              AND a.status IN ('pending', 'approved')
              AND a.consumed_at IS NULL;
            """
        )
        cur.execute(
            """
            WITH expired AS (
              SELECT *
              FROM browser_tasks
              WHERE
                status = 'running'
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at < now()
                AND retry_count >= max_retries
                AND execution_state = 'not_started'
              FOR UPDATE SKIP LOCKED
            ),
            inserted AS (
              INSERT INTO dead_letter_tasks (
                original_task_id,
                task_type,
                application_id,
                message_thread_id,
                input_json,
                last_error,
                retry_count,
                screenshot_url
              )
              SELECT
                id,
                task_type,
                application_id,
                message_thread_id,
                input_json,
                COALESCE(error_message, 'Lease expired and max retries exceeded.'),
                retry_count,
                screenshot_url
              FROM expired
              RETURNING original_task_id
            )
            UPDATE browser_tasks bt
            SET
              status = 'dead_letter',
              locked_by = NULL,
              lease_expires_at = NULL,
              finished_at = now(),
              error_message = COALESCE(error_message, '') || E'\nMoved to dead letter by watchdog.'
            FROM inserted
            WHERE bt.id = inserted.original_task_id
            RETURNING bt.id, bt.task_type, bt.retry_count, bt.max_retries;
            """
        )
        return cur.fetchall()


def main():
    print(f"Watchdog ID: {WATCHDOG_ID}")
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        uncertain = mark_uncertain_expired_tasks(conn)
        dead = dead_letter_expired_tasks(conn)
        requeued = requeue_expired_tasks(conn)
        conn.commit()

    print(f"Needs reconciliation: {len(uncertain)}")
    for row in uncertain:
        print("  reconcile:", row)

    print(f"Dead-lettered: {len(dead)}")
    for row in dead:
        print("  dead:", row)

    print(f"Requeued: {len(requeued)}")
    for row in requeued:
        print("  requeued:", row)


if __name__ == "__main__":
    main()
