import os
import socket
import uuid
import sys
from pathlib import Path
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn

WATCHDOG_ID = f"watchdog-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

DSN = database_dsn()

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
              AND COALESCE(execution_state, 'not_started') <> 'executing'
            RETURNING id, task_type, retry_count, max_retries;
            """
        )
        return cur.fetchall()

def dead_letter_expired_tasks(conn):
    with conn.cursor() as cur:
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
                AND COALESCE(execution_state, 'not_started') <> 'executing'
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
    with psycopg.connect(DSN, autocommit=False) as conn:
        dead = dead_letter_expired_tasks(conn)
        requeued = requeue_expired_tasks(conn)
        conn.commit()

    print(f"Dead-lettered: {len(dead)}")
    for row in dead:
        print("  dead:", row)

    print(f"Requeued: {len(requeued)}")
    for row in requeued:
        print("  requeued:", row)

if __name__ == "__main__":
    main()
