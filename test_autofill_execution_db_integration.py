"""Opt-in PostgreSQL integration test for the durable autofill state machine.

Run only against a disposable database whose DSN is supplied explicitly:
``JOBOS_TEST_DSN='...' JOBOS_RUN_DB_INTEGRATION=1 pytest -q test_autofill_execution_db_integration.py``.
The test never falls back to the developer's JobOS database.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest


TEST_DSN = os.getenv("JOBOS_TEST_DSN", "")
RUN = os.getenv("JOBOS_RUN_DB_INTEGRATION") == "1"


def test_migrations_054_058_support_one_durable_autofill_lifecycle():
    if not RUN:
        pytest.skip("set JOBOS_RUN_DB_INTEGRATION=1 and JOBOS_TEST_DSN for the disposable PostgreSQL integration test")
    if not TEST_DSN or "test" not in TEST_DSN.casefold():
        pytest.fail("JOBOS_TEST_DSN must explicitly name a disposable test database")

    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schema_migrations WHERE migration_id = '058_autofill_exact_action_scope.sql'")
        assert cur.fetchone(), "apply all migrations through 058 before this test"
        cur.execute("SELECT 1 FROM schema_migrations WHERE migration_id = '096_db_authority_final_invariants.sql'")
        assert cur.fetchone(), "apply all migrations through 096 before this DB-authority lifecycle test"
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'browser_tasks'")
        assert {"execution_state", "pinned_target_id", "autofill_plan_id"} <= {row[0] for row in cur.fetchall()}

        cur.execute("INSERT INTO applications (company, job_title) VALUES ('Test Company', 'Test Role') RETURNING id")
        application_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO generated_documents (application_id, doc_type, content, qa_status, approved)
               VALUES (%s, 'resume', 'verified text', 'pass', true) RETURNING id""",
            (application_id,),
        )
        document_id = cur.fetchone()[0]
        content_hash = hashlib.sha256(b"verified text").hexdigest()
        cur.execute(
            """INSERT INTO generated_document_artifacts
               (generated_document_id, application_id, artifact_type, file_path, filename, sha256)
               VALUES (%s, %s, 'resume', '/tmp/test-resume.docx', 'test-resume.docx', %s) RETURNING id""",
            (document_id, application_id, "a" * 64),
        )
        artifact_id = cur.fetchone()[0]

        action_scope_obj = {
            "profile_keys": ["personal.first_name"],
            "document_types": [],
            "sensitive_classes": [],
            "remembered_questions": [],
        }
        action_scope = json.dumps(action_scope_obj, sort_keys=True, separators=(",", ":"))
        cur.execute("SELECT pipeline_version FROM applications WHERE id=%s", (application_id,))
        pipeline_version = int(cur.fetchone()[0] or 0)
        plan_key = hashlib.sha256(b"root-db-integration-plan").hexdigest()
        action_scope_hash = hashlib.sha256(action_scope.encode("utf-8")).hexdigest()
        cur.execute(
            """INSERT INTO autofill_plans(
                   application_id,plan_key,pipeline_version,target_id,page_url,origin,page_fingerprint,
                   input_sha256,action_scope_sha256,action_scope_json,generated_document_id,artifact_id,
                   artifact_sha256,status)
               VALUES (%s,%s,%s,'target-1','https://jobs.example.test/job-1/apply','https://jobs.example.test',%s,
                       %s,%s,%s::jsonb,%s,%s,%s,'approved') RETURNING id""",
            (application_id, plan_key, pipeline_version, "b" * 64, "c" * 64, action_scope_hash,
             action_scope, document_id, artifact_id, "a" * 64),
        )
        plan_id = cur.fetchone()[0]
        payload = json.dumps({
            "autofill_plan_key": plan_key,
            "autofill_plan_id": str(plan_id),
            "expected_pipeline_version": pipeline_version,
            "expected_target_id": "target-1",
            "expected_origin": "https://jobs.example.test",
            "expected_initial_url": "https://jobs.example.test/job-1/apply",
            "expected_page_fingerprint": "b" * 64,
            "autofill_input_hash": "c" * 64,
            "document_id": str(document_id),
            "document_sha256": content_hash,
        })
        cur.execute(
            """INSERT INTO approval_requests
               (type, application_id, payload_json, status, approval_token_hash, token_expires_at,
                target_action, bound_document_id, bound_document_sha256, expected_origin, expected_target_id,
                bound_artifact_id, bound_artifact_sha256, bound_artifact_filename,
                expected_initial_url, expected_page_fingerprint, bound_autofill_input_hash,
                bound_autofill_action_scope, bound_pipeline_version, bound_autofill_plan_key, bound_autofill_plan_id)
               VALUES ('autofill_form', %s, %s::jsonb, 'approved', 'test-token', now() + interval '5 minutes',
                       'fill_application_form', %s, %s, 'https://jobs.example.test', 'target-1', %s, %s, 'test-resume.docx',
                       'https://jobs.example.test/job-1/apply', %s, %s, %s::jsonb, %s, %s, %s)
               RETURNING id""",
            (application_id, payload, document_id, content_hash, artifact_id, "a" * 64,
             "b" * 64, "c" * 64, action_scope, pipeline_version, plan_key, plan_id),
        )
        approval_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO browser_tasks
               (task_type, requested_by, application_id, status, approval_request_id,
                generated_document_id, document_sha256, expected_origin, bound_artifact_id,
                artifact_sha256, artifact_filename, expected_initial_url,
                expected_page_fingerprint, autofill_input_hash, autofill_action_scope, autofill_plan_id)
               VALUES ('fill_application_form', 'integration-test', %s, 'running', %s, %s, %s, 'https://jobs.example.test',
                       %s, %s, 'test-resume.docx', 'https://jobs.example.test/job-1/apply', %s, %s, %s::jsonb, %s)
               RETURNING id""",
            (application_id, approval_id, document_id, content_hash, artifact_id, "a" * 64,
             "b" * 64, "c" * 64, action_scope, plan_id),
        )
        task_id = cur.fetchone()[0]

        cur.execute(
            """UPDATE approval_requests SET status = 'executing', executing_task_id = %s
               WHERE id = %s AND status = 'approved' RETURNING id""",
            (task_id, approval_id),
        )
        assert cur.fetchone()
        cur.execute("UPDATE browser_tasks SET execution_state = 'executing', pinned_target_id = 'target-1' WHERE id = %s", (task_id,))
        cur.execute(
            """INSERT INTO autofill_action_journal
               (browser_task_id, approval_request_id, sequence_no, target_id, action_kind, target_ref, expected_value_sha256, status)
               VALUES (%s, %s, 1, 'target-1', 'fill', 'field-1', %s, 'started') RETURNING id""",
            (task_id, approval_id, hashlib.sha256(b"Ada").hexdigest()),
        )
        journal_id = cur.fetchone()[0]
        cur.execute("UPDATE autofill_action_journal SET status = 'verified', verified_at = now() WHERE id = %s", (journal_id,))
        cur.execute(
            """UPDATE approval_requests SET status = 'consumed', consumed_at = now(), consumed_by = 'integration-test'
               WHERE id = %s AND status = 'executing' AND executing_task_id = %s""",
            (approval_id, task_id),
        )
        cur.execute("UPDATE browser_tasks SET execution_state = 'completed', status = 'completed' WHERE id = %s", (task_id,))

        cur.execute("SELECT status, consumed_at IS NOT NULL FROM approval_requests WHERE id = %s", (approval_id,))
        assert cur.fetchone() == ("consumed", True)
        cur.execute("SELECT status FROM autofill_action_journal WHERE id = %s", (journal_id,))
        assert cur.fetchone() == ("verified",)
        cur.execute("SELECT execution_state FROM browser_tasks WHERE id = %s", (task_id,))
        assert cur.fetchone() == ("completed",)
        conn.rollback()
