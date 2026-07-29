import os
import socket
import uuid
import psycopg

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

WATCHDOG_ID = f"watchdog-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

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
