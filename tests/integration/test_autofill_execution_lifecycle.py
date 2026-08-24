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
        assert apply_migrations.apply(argparse.Namespace(dry_run=False, adopt_existing=False, through=58)) == 0
    finally:
        apply_migrations.connection_string = original
    return psycopg


def _record(db, *, approval_status: str = "approved", expires: str = "now() + interval '5 minutes'",
            idempotency_key: str | None = None, task_status: str = "running"):
    """Create the exact synthetic application/document/artifact/capability graph."""
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO applications (company, job_title) VALUES ('Fixture Co', 'Fixture Role') RETURNING id")
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
        action_scope = {
            "profile_keys": ["personal.first_name", "personal.email", "personal.phone"],
            "document_types": [], "sensitive_classes": [], "remembered_questions": [],
        }
        cur.execute(
            f"""INSERT INTO approval_requests
                (type, application_id, payload_json, status, approval_token_hash, token_expires_at,
                 target_action, bound_document_id, bound_document_sha256, expected_origin,
                 bound_artifact_id, bound_artifact_sha256, bound_artifact_filename,
                 expected_initial_url, expected_page_fingerprint, bound_autofill_input_hash,
                 bound_autofill_action_scope, idempotency_key)
               VALUES ('autofill_form', %s, '{{}}'::jsonb, %s, 'fixture-token', {expires},
                       'fill_application_form', %s, %s, 'https://jobs.example.test', %s, %s, 'fixture-resume.docx',
                       %s, %s, %s, %s::jsonb, %s) RETURNING id""",
            (application_id, approval_status, document_id, content_hash, artifact_id, artifact_hash,
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
