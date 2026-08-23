import os
import socket
import time
import uuid
import sys
from pathlib import Path
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn

WORKER_ID = f"fake-worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

DSN = database_dsn()

def claim_one_task(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH next_task AS (
              SELECT id
              FROM browser_tasks
              WHERE status = 'queued'
              ORDER BY
                CASE priority
                  WHEN 'high' THEN 1
                  WHEN 'normal' THEN 2
                  WHEN 'low' THEN 3
                  ELSE 4
                END,
                created_at ASC
              LIMIT 1
              FOR UPDATE SKIP LOCKED
            )
            UPDATE browser_tasks bt
            SET
              status = 'running',
              locked_by = %s,
              started_at = COALESCE(started_at, now()),
              lease_expires_at = now() + (timeout_seconds || ' seconds')::interval
            FROM next_task
            WHERE bt.id = next_task.id
            RETURNING bt.id, bt.task_type, bt.input_json, bt.timeout_seconds;
            """,
            (WORKER_ID,),
        )
        return cur.fetchone()

def complete_task(conn, task_id, result):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE browser_tasks
            SET
              status = 'completed',
              result_json = %s,
              finished_at = now(),
              lease_expires_at = NULL
            WHERE id = %s;
            """,
            (psycopg.types.json.Jsonb(result), task_id),
        )

def main():
    print(f"Worker ID: {WORKER_ID}")
    with psycopg.connect(DSN, autocommit=False) as conn:
        task = claim_one_task(conn)
        if not task:
            conn.commit()
            print("No queued task found.")
            return

        task_id, task_type, input_json, timeout_seconds = task
        if task_type in {"fill_application_form", "discover_linkedin_jobs"}:
            raise RuntimeError(
                "fake_worker is a queue-development fixture and cannot process autofill or LinkedIn discovery tasks. "
                "Use browser_queue_worker.py for the real bounded execution path."
            )
        conn.commit()

        print(f"Claimed task: {task_id}")
        print(f"Task type: {task_type}")
        print(f"Input: {input_json}")
        print(f"Timeout: {timeout_seconds}s")

        # Simulate doing browser work
        time.sleep(1)

        result = {
            "ok": True,
            "mode": "fake_worker",
            "message": "Task completed without calling OpenClaw.",
            "task_type": task_type,
            "input_seen": input_json,
            "worker_id": WORKER_ID,
        }

        with psycopg.connect(DSN, autocommit=False) as conn2:
            complete_task(conn2, task_id, result)
            conn2.commit()

        print(f"Completed task: {task_id}")

if __name__ == "__main__":
    main()
