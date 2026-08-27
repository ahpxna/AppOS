"""Fresh-DB integration gate for deterministic autofill lifecycle.

This suite is opt-in and destructive only to an explicitly named disposable
database. It applies migrations from zero and never starts OpenClaw, a model,
or a real browser. The transport is an in-memory fake.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.autofill.autofill_executor_v1 import BrowserTarget, TransportError
from services.autofill.autofill_planner_v1 import PlannedAction
from services.autofill.autofill_session_v1 import AutofillSession, SessionError, SnapshotState
from services.autofill.form_inspector_v1 import FormField
from services.common.autofill_action_scope import build_exact_action_scope, autofill_plan_key


ROOT = Path(__file__).resolve().parents[2]
TEST_DSN = os.getenv("JOBOS_TEST_DSN", "")
RUN = os.getenv("JOBOS_RUN_DB_INTEGRATION") == "1"


def _require_test_db():
    if not RUN:
        pytest.skip("set JOBOS_RUN_DB_INTEGRATION=1 and JOBOS_TEST_DSN for the disposable lifecycle gate")
    if not TEST_DSN or "test" not in TEST_DSN.casefold():
        pytest.fail("JOBOS_TEST_DSN must explicitly name a disposable database containing 'test'")
    return pytest.importorskip("psycopg")


def _load_worker():
    path = ROOT / "services" / "browser-controller" / "browser_queue_worker.py"
    spec = importlib.util.spec_from_file_location("jobos_test_browser_worker", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.DSN = TEST_DSN
    return module


def _load_reconcile():
    path = ROOT / "services" / "autofill" / "autofill_reconcile_v1.py"
    spec = importlib.util.spec_from_file_location("jobos_test_autofill_reconcile", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_watchdog():
    path = ROOT / "services" / "browser-controller" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("jobos_test_browser_watchdog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.DSN = TEST_DSN
    return module


@pytest.fixture(scope="session")
def db():
    psycopg = _require_test_db()
    # The database name guard above makes this destructive reset deliberate.
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    from scripts import apply_migrations
    original = apply_migrations.connection_string
    apply_migrations.connection_string = lambda: TEST_DSN
    try:
        assert apply_migrations.apply(argparse.Namespace(dry_run=False, adopt_existing=False, through=None)) == 0
    finally:
        apply_migrations.connection_string = original
    return psycopg


def _record(db, *, approval_status: str = "approved", expires: str = "now() + interval '5 minutes'",
            idempotency_key: str | None = None, task_status: str = "running"):
    """Create the exact synthetic application/document/artifact/capability graph."""
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO applications (company, job_title, current_step, job_url, jd_hash) VALUES ('Fixture Co', 'Fixture Role', 'awaiting_approval', 'https://jobs.example.test/apply?job=123', 'fixture-jd') RETURNING id")
        application_id = cur.fetchone()[0]
        content = "verified fixture resume"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        cur.execute(
            """INSERT INTO generated_documents (application_id, doc_type, content, qa_status, approved)
               VALUES (%s, 'resume', %s, 'pass', true) RETURNING id""",
            (application_id, content),
        )
        document_id = cur.fetchone()[0]
        artifact_hash = "a" * 64
        cur.execute(
            """INSERT INTO generated_document_artifacts
               (generated_document_id, application_id, artifact_type, file_path, filename, sha256)
               VALUES (%s, %s, 'resume', '/tmp/fixture-resume.docx', 'fixture-resume.docx', %s) RETURNING id""",
            (document_id, application_id, artifact_hash),
        )
        artifact_id = cur.fetchone()[0]
        page_url, fingerprint, input_hash = "https://jobs.example.test/apply?job=123", "b" * 64, "c" * 64
        # Use the production scope builder so integration fixtures evolve with
        # the exact action-scope contract instead of hard-coding v2/v3 JSON.
        action_scope = build_exact_action_scope([
            PlannedAction("fill", "first", "Ada", "personal.first_name", "fixture", "First name"),
            PlannedAction("fill", "email", "ada@example.test", "personal.email", "fixture", "Email"),
            PlannedAction("fill", "phone", "6095551234", "personal.phone", "fixture", "Phone"),
        ])
        approval_payload = {
            "application_job_url": page_url,
            "application_jd_hash": "fixture-jd",
            "expected_application_step": "awaiting_approval",
            "expected_upload_capabilities": [],
        }
        cur.execute(
            f"""INSERT INTO approval_requests
                (type, application_id, payload_json, status, approval_token_hash, token_expires_at,
                 target_action, bound_document_id, bound_document_sha256, expected_origin,
                 bound_artifact_id, bound_artifact_sha256, bound_artifact_filename,
                 expected_initial_url, expected_page_fingerprint, bound_autofill_input_hash,
                 bound_autofill_action_scope, idempotency_key)
               VALUES ('autofill_form', %s, %s::jsonb, %s, 'fixture-token', {expires},
                       'fill_application_form', %s, %s, 'https://jobs.example.test', %s, %s, 'fixture-resume.docx',
                       %s, %s, %s, %s::jsonb, %s) RETURNING id""",
            (application_id, json.dumps(approval_payload), approval_status, document_id, content_hash, artifact_id, artifact_hash,
             page_url, fingerprint, input_hash, json.dumps(action_scope), idempotency_key),
        )
        approval_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO browser_tasks
               (task_type, requested_by, application_id, status, approval_request_id,
                generated_document_id, document_sha256, expected_origin, bound_artifact_id,
                artifact_sha256, artifact_filename, expected_initial_url, expected_page_fingerprint,
                autofill_input_hash, autofill_action_scope, lease_expires_at)
               VALUES ('fill_application_form', 'fixture', %s, %s, %s, %s, %s,
                       'https://jobs.example.test', %s, %s, 'fixture-resume.docx', %s, %s, %s,
                       %s::jsonb, now() + interval '5 minutes') RETURNING id""",
            (application_id, task_status, approval_id, document_id, content_hash, artifact_id, artifact_hash,
             page_url, fingerprint, input_hash, json.dumps(action_scope)),
        )
        task_id = cur.fetchone()[0]
    return {
        "id": str(task_id), "application_id": str(application_id), "approval_request_id": str(approval_id),
        "generated_document_id": str(document_id), "document_sha256": content_hash,
        "bound_artifact_id": str(artifact_id), "artifact_sha256": artifact_hash,
        "artifact_filename": "fixture-resume.docx", "expected_origin": "https://jobs.example.test",
        "expected_initial_url": page_url, "expected_page_fingerprint": fingerprint,
        "autofill_input_hash": input_hash, "autofill_action_scope": action_scope,
        "input_json": {}, "timeout_seconds": 30,
        "retry_count": 0, "max_retries": 2,
    }


class FakeTransport:
    def __init__(self, *, url: str, fingerprint: str, fail_write: bool = False):
        self.url, self.fingerprint, self.fail_write = url, fingerprint, fail_write
        self.value, self.write_count = "", 0

    def resolve_target(self): return BrowserTarget("fixture-tab", self.url)
    def current_url(self, _target): return self.url
    def snapshot(self, _target): return {}
    def execute(self, _target, command):
        self.write_count += 1
        if self.fail_write:
            raise TransportError("synthetic worker crash during external write")
        self.value = command["value"]

    def state(self):
        return SnapshotState((FormField("name-ref", "First name", "textbox", self.value),), (), self.fingerprint)


class MultiFieldFakeTransport:
    def __init__(self, *, url: str, fingerprint: str):
        self.url, self.fingerprint = url, fingerprint
        self.values = {"first": "", "email": "", "phone": ""}
        self.write_refs: list[str] = []

    def resolve_target(self): return BrowserTarget("fixture-tab", self.url)
    def current_url(self, _target): return self.url
    def snapshot(self, _target): return {}
    def execute(self, _target, command):
        self.write_refs.append(command["target"])
        self.values[command["target"]] = command["value"]

    def state(self):
        return SnapshotState(
            (FormField("first", "First name", "textbox", self.values["first"]),
             FormField("email", "Email", "textbox", self.values["email"]),
             FormField("phone", "Phone", "textbox", self.values["phone"])),
            (), self.fingerprint,
        )


def _binding(worker, db, task):
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        return worker.require_bound_approval(cur, task)


def test_happy_path_calls_production_durable_functions(db):
    worker, task = _load_worker(), _record(db)
    binding, transport = _binding(worker, db, task), FakeTransport(url=task["expected_initial_url"], fingerprint=task["expected_page_fingerprint"])
    action = PlannedAction("fill", "name-ref", "Ada", "personal.first_name", "fixture", "First name")
    session = AutofillSession(
        transport=transport, expected_origin=task["expected_origin"], expected_initial_url=task["expected_initial_url"],
        expected_page_fingerprint=task["expected_page_fingerprint"], snapshot_state=lambda _id: transport.state(),
        origin_allowed=lambda _url: None, begin_execution=lambda target: worker.durable_begin_execution(task, binding, target),
        before_action=lambda item, target: worker.durable_journal_start(task, binding, item, target),
        after_verified=worker.durable_journal_verified, after_failed=worker.durable_journal_failed,
    )
    result = session.execute(lambda _state: [action])
    worker.durable_finish_execution(task, binding, result)
    assert transport.write_count == 1 and result.status == "completed"
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, consumed_at IS NOT NULL FROM approval_requests WHERE id = %s", (task["approval_request_id"],))
        assert cur.fetchone() == ("consumed", True)
        cur.execute("SELECT status FROM autofill_action_journal WHERE browser_task_id = %s", (task["id"],))
        assert cur.fetchone() == ("verified",)


def test_wrong_job_or_fingerprint_writes_zero(db):
    task = _record(db)
    for url, fingerprint in (("https://jobs.example.test/apply?job=456", task["expected_page_fingerprint"]),
                             (task["expected_initial_url"], "different")):
        transport = FakeTransport(url=url, fingerprint=fingerprint)
        session = AutofillSession(
            transport=transport, expected_origin=task["expected_origin"], expected_initial_url=task["expected_initial_url"],
            expected_page_fingerprint=task["expected_page_fingerprint"], snapshot_state=lambda _id: transport.state(),
            origin_allowed=lambda _url: None, begin_execution=lambda _id: pytest.fail("must not begin"),
            before_action=lambda *_args: pytest.fail("must not journal"), after_verified=lambda *_args: None,
            after_failed=lambda *_args: None,
        )
        with pytest.raises(SessionError): session.execute(lambda _state: [])
        assert transport.write_count == 0


def test_changed_profile_legal_document_or_artifact_refuses_before_transport(db):
    worker, task = _load_worker(), _record(db)
    binding = _binding(worker, db, task)
    transport = FakeTransport(url=task["expected_initial_url"], fingerprint=task["expected_page_fingerprint"])
    with pytest.raises(worker.PermanentTaskError): worker.require_current_input_hash(binding, "changed-profile")
    with pytest.raises(worker.PermanentTaskError): worker.require_current_input_hash(binding, "changed-confirmed-legal-answer")
    changed_document = dict(task, document_sha256="changed-document")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        with pytest.raises(worker.PermanentTaskError): worker.require_bound_approval(cur, changed_document)
        changed_artifact = dict(task, artifact_sha256="changed-artifact")
        with pytest.raises(worker.PermanentTaskError): worker.require_bound_approval(cur, changed_artifact)
    assert transport.write_count == 0


def test_expired_capability_never_writes_and_idempotency_can_be_reissued(db):
    worker, task = _load_worker(), _record(db, expires="now() - interval '1 minute'", idempotency_key="expired-fixture")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        with pytest.raises(worker.PermanentTaskError): worker.require_bound_approval(cur, task)
        cur.execute("UPDATE approval_requests SET status = 'expired' WHERE id = %s", (task["approval_request_id"],))
        cur.execute(
            """
            INSERT INTO approval_requests (
                type,
                status,
                payload_json,
                approval_token_hash,
                token_expires_at,
                idempotency_key
            )
            VALUES (
                'fit_review',
                'pending',
                '{}'::jsonb,
                'fixture-token',
                now() + interval '5 minutes',
                'expired-fixture'
            )
            """
        )


def test_crash_with_journal_requires_reconciliation_and_is_never_requeued(db):
    worker, task = _load_worker(), _record(db)
    binding, transport = _binding(worker, db, task), FakeTransport(url=task["expected_initial_url"], fingerprint=task["expected_page_fingerprint"], fail_write=True)
    action = PlannedAction("fill", "name-ref", "Ada", "personal.first_name", "fixture", "First name")
    worker.durable_begin_execution(task, binding, "fixture-tab")
    journal_id = worker.durable_journal_start(task, binding, action, "fixture-tab")
    with pytest.raises(TransportError): transport.execute("fixture-tab", {"action": "fill", "target": "name-ref", "value": "Ada"})
    worker.durable_mark_reconciliation(task, "fixture-tab", "synthetic crash after journal")
    assert transport.write_count == 1
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("needs_reconciliation",)
        cur.execute("SELECT status FROM autofill_action_journal WHERE id = %s", (journal_id,))
        assert cur.fetchone() == ("started",)


def test_reconciliation_closes_old_capability_and_allows_fresh_one(db):
    worker, reconcile, task = _load_worker(), _load_reconcile(), _record(db, idempotency_key="reconcile-fixture")
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    worker.durable_mark_reconciliation(task, "fixture-tab", "synthetic uncertain browser write")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        reconcile.close(cur, task["id"])
        cur.execute("SELECT status, executing_task_id FROM approval_requests WHERE id = %s", (task["approval_request_id"],))
        assert cur.fetchone() == ("expired", None)
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("failed", "partial")
        cur.execute("SELECT status FROM application_attempts WHERE browser_task_id = %s", (task["id"],))
        assert cur.fetchone() == ("reconciled",)
        cur.execute("SELECT current_step FROM applications WHERE id = %s", (task["application_id"],))
        assert cur.fetchone() == ("application_form_ready",)
        # The active-idempotency index intentionally excludes expired rows.
        cur.execute(
            """
            INSERT INTO approval_requests (
                type,
                status,
                payload_json,
                approval_token_hash,
                token_expires_at,
                idempotency_key
            )
            VALUES (
                'fit_review',
                'pending',
                '{}'::jsonb,
                'fixture-token',
                now() + interval '5 minutes',
                'reconcile-fixture'
            )
            """
        )


def test_lease_reaper_retries_only_provably_pre_io_execution(db):
    worker, task = _load_worker(), _record(db)
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE browser_tasks SET lease_expires_at = now() - interval '1 minute' WHERE id = %s", (task["id"],))
        assert worker.reap_expired_leases(cur) == 1
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("queued", "not_started")
        cur.execute("SELECT status FROM approval_requests WHERE id = %s", (task["approval_request_id"],))
        assert cur.fetchone() == ("approved",)


def test_exhausted_pre_io_execution_closes_capability_instead_of_requeueing(db):
    worker, task = _load_worker(), _record(db)
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE browser_tasks
                  SET retry_count = max_retries, lease_expires_at = now() - interval '1 minute'
                WHERE id = %s""",
            (task["id"],),
        )
        assert worker.reap_expired_leases(cur) == 1
        conn.commit()
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("failed", "not_started")
        cur.execute("SELECT status FROM approval_requests WHERE id = %s", (task["approval_request_id"],))
        assert cur.fetchone() == ("expired",)


def test_retry_helper_never_requeues_after_action_journal_exists(db):
    worker, task = _load_worker(), _record(db)
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    action = PlannedAction("fill", "name-ref", "Ada", "personal.first_name", "fixture", "First name")
    worker.durable_journal_start(task, binding, action, "fixture-tab")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        status = worker.requeue_or_fail(cur, task, "synthetic unexpected exception")
        conn.commit()
    assert status == "failed"
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("failed", "needs_reconciliation")
        cur.execute("SELECT status FROM approval_requests WHERE id = %s", (task["approval_request_id"],))
        assert cur.fetchone() == ("executing",)


def test_watchdog_never_requeues_partial_browser_state(db):
    watchdog, task = _load_watchdog(), _record(db)
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE browser_tasks
                  SET execution_state = 'partial', lease_expires_at = now() - interval '1 minute'
                WHERE id = %s""",
            (task["id"],),
        )
        conn.commit()
    with db.connect(TEST_DSN) as conn:
        uncertain = watchdog.mark_uncertain_expired_tasks(conn)
        requeued = watchdog.requeue_expired_tasks(conn)
        conn.commit()
    assert len(uncertain) == 1
    assert requeued == []
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("failed", "needs_reconciliation")


def _patch_multifield_worker(worker, monkeypatch, seeded, transport):
    monkeypatch.setattr(worker, "OpenClawTransport", lambda **_kwargs: transport)
    monkeypatch.setattr(worker, "snapshot_state", lambda actual_transport, _target: actual_transport.state())
    monkeypatch.setattr(worker, "check_domain", lambda _cur, _url: None)
    monkeypatch.setattr(worker, "require_ats_autofill_capability", lambda _cur, _application_id: {
        "fill": True, "check": True, "select": True, "upload": True,
    })
    monkeypatch.setattr(worker, "load_autofill_context", lambda *_args, **_kwargs: SimpleNamespace(
        profile={"personal": {"first_name": "Ada", "email": "ada@example.test", "phone": "6095551234"}},
        sensitive_answers={}, remembered_answers={}, input_hash=seeded["autofill_input_hash"],
    ))
    monkeypatch.setattr(worker, "emit_trace", lambda *_args, **_kwargs: None)


def test_production_worker_path_claims_exact_approval_journals_each_write_and_completes(db, monkeypatch):
    worker = _load_worker()

    # Isolate this production-path test from queued seed/migration tasks.
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE browser_tasks
            SET status = 'failed',
                error_message = 'integration test isolation',
                finished_at = now(),
                locked_by = NULL,
                lease_expires_at = NULL
            WHERE status = 'queued'
            """
        )
        conn.commit()

    seeded = _record(db, task_status="queued")
    transport = MultiFieldFakeTransport(
        url=seeded["expected_initial_url"], fingerprint=seeded["expected_page_fingerprint"],
    )
    _patch_multifield_worker(worker, monkeypatch, seeded, transport)

    with db.connect(TEST_DSN) as conn:
        assert worker.process_one(conn) is True

    assert transport.write_refs == ["first", "email", "phone"]
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (seeded["id"],))
        assert cur.fetchone() == ("completed", "completed")
        cur.execute("SELECT status, consumed_at IS NOT NULL FROM approval_requests WHERE id = %s", (seeded["approval_request_id"],))
        assert cur.fetchone() == ("consumed", True)
        cur.execute("SELECT target_ref, status FROM autofill_action_journal WHERE browser_task_id = %s", (seeded["id"],))
        journal = cur.fetchall()
        assert {row[0] for row in journal} == {"first", "email", "phone"}
        assert len(journal) == 3
        assert all(row[1] == "verified" for row in journal)


def test_production_fill_handler_never_calls_openclaw_agent(db, monkeypatch):
    worker = _load_worker()
    task = _record(db)
    transport = MultiFieldFakeTransport(
        url=task["expected_initial_url"], fingerprint=task["expected_page_fingerprint"],
    )
    _patch_multifield_worker(worker, monkeypatch, task, transport)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("fill_application_form must not call openclaw_agent")

    monkeypatch.setattr(worker, "openclaw_agent", fail_if_called)
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        result = worker.handle_fill_application_form(cur, task)

    assert result["status"] == "completed"
    assert transport.write_refs == ["first", "email", "phone"]


def test_fill_handler_is_deterministic_handler():
    worker = _load_worker()
    assert worker.HANDLERS["fill_application_form"] is worker.handle_fill_application_form


def test_review_materialization_failure_does_not_reclassify_completed_browser_execution(db, monkeypatch):
    worker = _load_worker()
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE browser_tasks
                  SET status = 'failed', error_message = 'integration test isolation',
                      finished_at = now(), locked_by = NULL, lease_expires_at = NULL
                WHERE status = 'queued'"""
        )
        conn.commit()

    seeded = _record(db, task_status="queued")
    transport = MultiFieldFakeTransport(
        url=seeded["expected_initial_url"], fingerprint=seeded["expected_page_fingerprint"],
    )
    _patch_multifield_worker(worker, monkeypatch, seeded, transport)

    from services.review import review_service_v1 as review

    def fail_materialization(*_args, **_kwargs):
        raise RuntimeError("synthetic review materialization failure")

    monkeypatch.setattr(review, "ensure_autofill_review", fail_materialization)
    with db.connect(TEST_DSN) as conn:
        assert worker.process_one(conn) is True

    assert transport.write_refs == ["first", "email", "phone"]
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (seeded["id"],))
        assert cur.fetchone() == ("completed", "completed")
        cur.execute("SELECT status, consumed_at IS NOT NULL FROM approval_requests WHERE id = %s", (seeded["approval_request_id"],))
        assert cur.fetchone() == ("consumed", True)
        cur.execute("SELECT count(*) FROM human_review_items WHERE browser_task_id = %s", (seeded["id"],))
        assert cur.fetchone()[0] == 0


def test_reconciliation_close_accepts_already_consumed_capability_without_reopening_it(db):
    worker, reconcile, task = _load_worker(), _load_reconcile(), _record(db)
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    result = SimpleNamespace(
        status="completed", target_id="fixture-tab",
        verified_refs=("name-ref",), failed_refs=(),
    )
    worker.durable_finish_execution(task, binding, result)
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE browser_tasks
                  SET status = 'failed', execution_state = 'needs_reconciliation'
                WHERE id = %s""",
            (task["id"],),
        )
        conn.commit()
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        reconcile.close(cur, task["id"])
        conn.commit()
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, consumed_at IS NOT NULL, executing_task_id FROM approval_requests WHERE id = %s", (task["approval_request_id"],))
        assert cur.fetchone() == ("consumed", True, None)
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id = %s", (task["id"],))
        assert cur.fetchone() == ("failed", "partial")
        cur.execute("SELECT status FROM application_attempts WHERE browser_task_id = %s", (task["id"],))
        assert cur.fetchone() == ("reconciled",)


def test_application_execution_fence_is_durable_before_browser_io(db):
    worker, task = _load_worker(), _record(db)
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_step FROM applications WHERE id=%s", (task["application_id"],))
        assert cur.fetchone() == ("autofill_executing",)
        cur.execute("SELECT status FROM approval_requests WHERE id=%s", (task["approval_request_id"],))
        assert cur.fetchone() == ("executing",)


def test_post_io_lifecycle_race_becomes_reconciliation_not_completed(db):
    worker, task = _load_worker(), _record(db)
    binding = _binding(worker, db, task)
    worker.durable_begin_execution(task, binding, "fixture-tab")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE applications SET current_step='abandoned', status='abandoned' WHERE id=%s", (task["application_id"],))
        conn.commit()
    result = SimpleNamespace(status="completed", target_id="fixture-tab", verified_refs=("first",), failed_refs=())
    with pytest.raises(worker.PermanentTaskError, match="lifecycle changed after deterministic browser I/O"):
        worker.durable_finish_execution(task, binding, result)
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_step FROM applications WHERE id=%s", (task["application_id"],))
        assert cur.fetchone() == ("abandoned",)
        cur.execute("SELECT status, execution_state FROM browser_tasks WHERE id=%s", (task["id"],))
        assert cur.fetchone() == ("running", "needs_reconciliation")
        cur.execute("SELECT status FROM approval_requests WHERE id=%s", (task["approval_request_id"],))
        assert cur.fetchone() == ("consumed",)


def test_denied_parent_restores_form_ready_and_closes_delegated_children(db):
    approval = _load_module("approval_service_lifecycle", ROOT / "services" / "approval" / "approval_service_v1.py")
    task = _record(db, approval_status="pending")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE applications SET current_step='awaiting_approval' WHERE id=%s", (task["application_id"],))
        plan_key = "plan-fixture"
        cur.execute("UPDATE approval_requests SET payload_json = jsonb_build_object('application_job_url','https://jobs.example.test/apply?job=123','application_jd_hash','fixture-jd','expected_application_step','awaiting_approval','autofill_plan_key',%s) WHERE id=%s", (plan_key, task["approval_request_id"]))
        cur.execute("""INSERT INTO approval_requests(type,application_id,payload_json,status,approval_token_hash,token_expires_at)
                       VALUES ('privileged_upload_document',%s,%s,'approved','child-token',now()+interval '5 minutes') RETURNING id""",
                    (task["application_id"], json.dumps({"delegated_to_autofill": True, "autofill_plan_key": plan_key, "parent_approval_request_id": task["approval_request_id"]})))
        child_id = cur.fetchone()[0]
        conn.commit()
    with db.connect(TEST_DSN) as conn:
        out = approval.decide_request_by_id(conn, task["approval_request_id"], decision="deny", note="no", actor="test")
        assert out["ok"] is True
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_step FROM applications WHERE id=%s", (task["application_id"],))
        assert cur.fetchone() == ("application_form_ready",)
        cur.execute("SELECT status FROM approval_requests WHERE id=%s", (child_id,))
        assert cur.fetchone() == ("expired",)



def test_missing_delegated_child_is_repaired_and_bound_to_exact_parent(db):
    from services.approval import approval_service_v1 as approval

    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO applications(company,job_title,current_step,job_url,jd_hash) VALUES ('Repair Co','Role','awaiting_approval','https://jobs.example.test/apply','jd-repair') RETURNING id")
        app_id = cur.fetchone()[0]
        package = {
            "field_ref": "resume", "field_label": "Resume", "document_type": "resume",
            "generated_document_id": "00000000-0000-0000-0000-000000000001",
            "artifact_id": "00000000-0000-0000-0000-000000000002",
            "file_path": "/tmp/resume.pdf", "filename": "resume.pdf", "sha256": "a" * 64,
            "source_jd_hash": "jd-repair", "application_jd_hash": "jd-repair",
            "autofill_plan_key": "repair-plan", "delegated_to_autofill": True,
            "target_id": "tab", "expected_url": "https://jobs.example.test/apply",
            "expected_origin": "https://jobs.example.test", "expected_page_fingerprint": "b" * 64,
        }
        parent_payload = {
            "autofill_plan_key": "repair-plan",
            "expected_upload_capabilities": [{"field_ref": "resume", "document_type": "resume", "artifact_id": package["artifact_id"], "sha256": "a" * 64}],
            "delegated_upload_packages": [package],
        }
        cur.execute("""INSERT INTO approval_requests(type,application_id,payload_json,status,approval_token_hash,token_expires_at)
                       VALUES ('autofill_form',%s,%s,'pending','parent-token',now()+interval '5 minutes') RETURNING id::text""",
                    (app_id, json.dumps(parent_payload)))
        parent_id = cur.fetchone()[0]
        assert approval.queue_ready_autofill_for_plan(cur, application_id=str(app_id), plan_key="repair-plan", actor="test") is False
        cur.execute("""SELECT payload_json->>'parent_approval_request_id' FROM approval_requests
                       WHERE application_id=%s AND type='privileged_upload_document'""", (app_id,))
        assert cur.fetchone() == (parent_id,)
        conn.commit()


def test_same_plan_new_parent_gets_distinct_upload_child(db):
    from services.approval import approval_service_v1 as approval

    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO applications(company,job_title,current_step,job_url,jd_hash) VALUES ('Parent Co','Role','awaiting_approval','https://jobs.example.test/apply','jd-parent') RETURNING id")
        app_id = cur.fetchone()[0]
        base_package = {
            "field_ref": "resume", "document_type": "resume", "artifact_id": "art",
            "sha256": "a" * 64, "autofill_plan_key": "same-plan", "filename": "resume.pdf",
        }
        payload = {"delegated_upload_packages": [base_package], "expected_upload_capabilities": []}
        cur.execute("""INSERT INTO approval_requests(type,application_id,payload_json,status,approval_token_hash,token_expires_at,created_at)
                       VALUES ('autofill_form',%s,%s,'denied','p1',now()+interval '5 minutes',now()-interval '1 minute') RETURNING id::text""", (app_id, json.dumps({**payload, "autofill_plan_key": "same-plan"})))
        parent_a = cur.fetchone()[0]
        approval._repair_delegated_children_for_parent(cur, application_id=str(app_id), parent_request_id=parent_a, payload=payload)
        cur.execute("""INSERT INTO approval_requests(type,application_id,payload_json,status,approval_token_hash,token_expires_at)
                       VALUES ('autofill_form',%s,%s,'pending','p2',now()+interval '5 minutes') RETURNING id::text""", (app_id, json.dumps({**payload, "autofill_plan_key": "same-plan"})))
        parent_b = cur.fetchone()[0]
        approval._repair_delegated_children_for_parent(cur, application_id=str(app_id), parent_request_id=parent_b, payload=payload)
        cur.execute("""SELECT payload_json->>'parent_approval_request_id', count(*) FROM approval_requests
                       WHERE application_id=%s AND type='privileged_upload_document'
                       GROUP BY 1 ORDER BY 1""", (app_id,))
        rows = cur.fetchall()
        assert rows == sorted([(parent_a, 1), (parent_b, 1)])
        conn.commit()



def _configure_delegated_gate(db, *, child_status: str):
    """Seed one exact approved parent and one exact child without a queued browser task."""
    task = _record(db, approval_status="approved", task_status="queued")
    plan_key = autofill_plan_key(
        application_id=task["application_id"], page_url=task["expected_initial_url"],
        page_fingerprint=task["expected_page_fingerprint"], input_hash=task["autofill_input_hash"],
        action_scope=task["autofill_action_scope"],
    )
    spec = {
        "field_ref": "resume-upload", "document_type": "resume",
        "artifact_id": task["bound_artifact_id"], "sha256": task["artifact_sha256"],
    }
    payload = {
        "application_job_url": task["expected_initial_url"],
        "application_jd_hash": "fixture-jd",
        "expected_application_step": "awaiting_approval",
        "document_id": task["generated_document_id"],
        "document_sha256": task["document_sha256"],
        "artifact_id": task["bound_artifact_id"],
        "artifact_sha256": task["artifact_sha256"],
        "artifact_filename": task["artifact_filename"],
        "expected_origin": task["expected_origin"],
        "expected_initial_url": task["expected_initial_url"],
        "expected_page_fingerprint": task["expected_page_fingerprint"],
        "autofill_input_hash": task["autofill_input_hash"],
        "autofill_action_scope": task["autofill_action_scope"],
        "autofill_plan_key": plan_key,
        "expected_upload_capabilities": [spec],
        # Existing-child lifecycle tests intentionally leave repair packages empty.
        "delegated_upload_packages": [],
    }
    child_payload = {
        **spec,
        "parent_approval_request_id": task["approval_request_id"],
        "delegated_to_autofill": True,
        "autofill_plan_key": plan_key,
    }
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM browser_tasks WHERE id=%s", (task["id"],))
        cur.execute("UPDATE approval_requests SET payload_json=%s::jsonb WHERE id=%s", (json.dumps(payload), task["approval_request_id"]))
        cur.execute(
            """INSERT INTO approval_requests(type,application_id,payload_json,status,approval_token_hash,token_expires_at)
               VALUES ('privileged_upload_document',%s,%s::jsonb,%s,%s,now()+interval '5 minutes') RETURNING id::text""",
            (task["application_id"], json.dumps(child_payload), child_status, f"child-{child_status}"),
        )
        child_id = cur.fetchone()[0]
        conn.commit()
    return task, plan_key, child_id


def test_parent_approved_child_pending_does_not_queue_browser_task(db):
    from services.approval import approval_service_v1 as approval
    task, plan_key, _child_id = _configure_delegated_gate(db, child_status="pending")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        assert approval.queue_ready_autofill_for_plan(cur, application_id=task["application_id"], plan_key=plan_key, actor="db-test") is False
        cur.execute("SELECT count(*) FROM browser_tasks WHERE approval_request_id=%s", (task["approval_request_id"],))
        assert cur.fetchone() == (0,)
        conn.commit()


def test_parent_approved_child_denied_queues_once_and_upload_remains_nonexecutable(db):
    from services.approval import approval_service_v1 as approval
    task, plan_key, child_id = _configure_delegated_gate(db, child_status="denied")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        assert approval.queue_ready_autofill_for_plan(cur, application_id=task["application_id"], plan_key=plan_key, actor="db-test") is True
        assert approval.queue_ready_autofill_for_plan(cur, application_id=task["application_id"], plan_key=plan_key, actor="db-test") is False
        cur.execute("SELECT count(*) FROM browser_tasks WHERE approval_request_id=%s", (task["approval_request_id"],))
        assert cur.fetchone() == (1,)
        cur.execute("SELECT status FROM approval_requests WHERE id=%s", (child_id,))
        assert cur.fetchone() == ("denied",)
        conn.commit()


def test_parent_approved_child_approved_queues_exactly_one_parent_task(db):
    from services.approval import approval_service_v1 as approval
    task, plan_key, child_id = _configure_delegated_gate(db, child_status="approved")
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        assert approval.queue_ready_autofill_for_plan(cur, application_id=task["application_id"], plan_key=plan_key, actor="db-test") is True
        assert approval.queue_ready_autofill_for_plan(cur, application_id=task["application_id"], plan_key=plan_key, actor="db-test") is False
        cur.execute("SELECT count(*) FROM browser_tasks WHERE approval_request_id=%s", (task["approval_request_id"],))
        assert cur.fetchone() == (1,)
        cur.execute("SELECT payload_json->>'parent_approval_request_id', status FROM approval_requests WHERE id=%s", (child_id,))
        assert cur.fetchone() == (task["approval_request_id"], "approved")
        conn.commit()


def test_fit_review_redemption_transitions_atomically_and_replay_is_terminal(db):
    from services.approval import approval_service_v1 as approval
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO applications(company,job_title,current_step,status)
                 VALUES ('Fit Gate Co','Role','awaiting_fit_review','active') RETURNING id::text"""
        )
        app_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO approval_requests(type,application_id,payload_json,status,approval_token_hash,token_expires_at)
                 VALUES ('fit_review',%s,'{}'::jsonb,'pending','fit-review-token',now()+interval '5 minutes')
                 RETURNING id::text""",
            (app_id,),
        )
        request_id = cur.fetchone()[0]
        conn.commit()
    with db.connect(TEST_DSN) as conn:
        outcome = approval.decide_request_by_id(conn, request_id, decision="approve", note="go", actor="db-test")
        assert outcome["ok"] is True
        replay = approval.decide_request_by_id(conn, request_id, decision="approve", note="again", actor="db-test")
        assert replay["ok"] is False
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_step FROM applications WHERE id=%s", (app_id,))
        assert cur.fetchone() == ("fit_analyzed",)
        cur.execute("SELECT count(*) FROM pipeline_events WHERE application_id=%s AND to_step='fit_analyzed'", (app_id,))
        assert cur.fetchone() == (1,)


def test_document_generation_attempt_reuses_completed_and_recovers_after_bounded_lease(db):
    from services.control_plane.document_attempts import (
        DocumentAttemptBusyError, claim, complete, fail,
    )
    manifest = {"generator_version": "db-fixture", "jd_sha256": "f" * 64, "asset_ids": []}
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO applications(company,job_title,current_step) VALUES ('Doc Attempt Co','Role','fit_analyzed') RETURNING id::text")
        app_id = cur.fetchone()[0]
        cur.execute("INSERT INTO generated_documents(application_id,doc_type,content) VALUES (%s,'resume','fixture') RETURNING id::text", (app_id,))
        document_id = cur.fetchone()[0]
        first = claim(cur, application_id=app_id, doc_type="resume", request_kind="generation", input_manifest=manifest)
        complete(cur, attempt_id=first.id, document_id=document_id)
        uncertain = claim(cur, application_id=app_id, doc_type="cover_letter", request_kind="generation",
                          input_manifest={**manifest, "doc_type": "cover_letter"}, lease_seconds=300)
        fail(cur, attempt_id=uncertain.id, error="simulated uncertain provider outcome", uncertain=True,
             retry_delay_seconds=60)
        conn.commit()
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        reused = claim(cur, application_id=app_id, doc_type="resume", request_kind="generation", input_manifest=manifest)
        assert reused.id == first.id and reused.completed_document_id == document_id
        with pytest.raises(DocumentAttemptBusyError, match="recovery lease"):
            claim(cur, application_id=app_id, doc_type="cover_letter", request_kind="generation",
                  input_manifest={**manifest, "doc_type": "cover_letter"})
        cur.execute("UPDATE document_generation_attempts SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
                    (uncertain.id,))
        recovered = claim(cur, application_id=app_id, doc_type="cover_letter", request_kind="generation",
                          input_manifest={**manifest, "doc_type": "cover_letter"})
        assert recovered.id == uncertain.id
        assert recovered.attempt_count == 2
        cur.execute("UPDATE document_generation_attempts SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
                    (recovered.id,))
        recovered_again = claim(cur, application_id=app_id, doc_type="cover_letter", request_kind="generation",
                                input_manifest={**manifest, "doc_type": "cover_letter"})
        assert recovered_again.id == recovered.id
        assert recovered_again.attempt_count == 3
        conn.rollback()
