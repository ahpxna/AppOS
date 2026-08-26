from __future__ import annotations
import hashlib
import os
import uuid
import pytest

psycopg = pytest.importorskip("psycopg")
TEST_DSN = os.getenv("JOBOS_TEST_DSN", "")
RUN = os.getenv("JOBOS_RUN_DB_INTEGRATION", "") == "1"


def _require_test_db():
    if not RUN:
        pytest.skip("set JOBOS_RUN_DB_INTEGRATION=1 and JOBOS_TEST_DSN")
    if not TEST_DSN or "test" not in TEST_DSN.casefold():
        pytest.fail("JOBOS_TEST_DSN must name a disposable database containing 'test'")


@pytest.fixture()
def db():
    _require_test_db(); return psycopg


def _fixture(db):
    app_id, doc_id = str(uuid.uuid4()), str(uuid.uuid4())
    jd_hash = "d" * 64
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO applications(
                           id, source, company, job_title, current_step, status,
                           jd_text, jd_hash, created_at, updated_at)
                       VALUES (%s, 'test', 'Review Fixture Co', 'Security Engineer',
                               'docs_verified', 'active', 'fixture job description', %s, now(), now())""",
                    (app_id, jd_hash))
        cur.execute("""INSERT INTO generated_documents(
                           id, application_id, doc_type, version, content, qa_status, approved,
                           source_jd_hash, created_at)
                       VALUES (%s, %s, 'resume', 1, 'truth checked resume', 'pass', false, %s, now())""",
                    (doc_id, app_id, jd_hash))
        conn.commit()
    return app_id, doc_id



def _register_pdf_artifact(db, app_id, doc_id, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% JobOS integration fixture\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO generated_document_artifacts(
                   generated_document_id, application_id, artifact_type, file_path, filename, sha256)
               VALUES (%s, %s, 'resume', %s, %s, %s)
               RETURNING id::text;""",
            (doc_id, app_id, str(path.resolve()), path.name, digest),
        )
        artifact_id = cur.fetchone()[0]
        conn.commit()
    return artifact_id, digest

def _cleanup(db, app_id):
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM applications WHERE id = %s", (app_id,)); conn.commit()


def test_document_review_rejects_stale_content(db, monkeypatch):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    try:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            item_id = review.ensure_document_review(cur, doc_id); conn.commit()
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("UPDATE generated_documents SET content = content || ' changed' WHERE id = %s", (doc_id,)); conn.commit()
        with db.connect(TEST_DSN) as conn:
            with pytest.raises(review.ReviewError, match="changed after review"):
                review.decide_item(conn, item_id, decision="approve", actor="integration-test")
            conn.rollback()
    finally:
        _cleanup(db, app_id)


def test_document_review_approves_current_qa_passed_content(db, monkeypatch, tmp_path):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    try:
        artifact_id, _digest = _register_pdf_artifact(
            db, app_id, doc_id, tmp_path / "review" / "resume.pdf"
        )
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            item_id = review.ensure_document_review(cur, doc_id)
            conn.commit()
        with db.connect(TEST_DSN) as conn:
            result = review.decide_item(
                conn, item_id, decision="approve", actor="integration-test"
            )
            conn.commit()
        assert result["status"] == "approved"
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT gd.approved, a.approved_resume_id::text,
                          a.approved_resume_artifact_id::text
                     FROM generated_documents gd
                     JOIN applications a ON a.id = gd.application_id
                    WHERE gd.id = %s""",
                (doc_id,),
            )
            approved, approved_doc_id, approved_artifact_id = cur.fetchone()
            assert approved is True
            assert approved_doc_id == doc_id
            assert approved_artifact_id == artifact_id
    finally:
        _cleanup(db, app_id)


def test_missing_pdf_review_is_idempotent_across_syncs(db, monkeypatch):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    try:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            first = review.ensure_document_review(cur, doc_id)
            second = review.ensure_document_review(cur, doc_id)
            cur.execute(
                """SELECT id::text, status FROM human_review_items
                     WHERE application_id = %s AND item_type = 'document_review'
                       AND status IN ('pending','needs_revision')""",
                (app_id,),
            )
            active = cur.fetchall()
            conn.commit()
        assert first == second
        assert active == [(first, "needs_revision")]
    finally:
        _cleanup(db, app_id)


def test_new_document_version_supersedes_old_active_slot(db, monkeypatch):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_v1 = _fixture(db)
    doc_v2 = str(uuid.uuid4())
    try:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            old_item = review.ensure_document_review(cur, doc_v1)
            cur.execute(
                """INSERT INTO generated_documents(
                       id, application_id, doc_type, version, content, qa_status, approved, created_at)
                   VALUES (%s, %s, 'resume', 2, 'truth checked resume v2', 'pass', false, now())""",
                (doc_v2, app_id),
            )
            new_item = review.ensure_document_review(cur, doc_v2)
            cur.execute(
                """SELECT id::text, status, generated_document_id::text
                     FROM human_review_items
                    WHERE id IN (%s, %s) ORDER BY created_at, id""",
                (old_item, new_item),
            )
            rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            conn.commit()
        assert new_item != old_item
        assert rows[old_item] == ("expired", doc_v1)
        assert rows[new_item] == ("needs_revision", doc_v2)
    finally:
        _cleanup(db, app_id)


def test_human_revision_request_survives_sync_until_document_changes(db, monkeypatch, tmp_path):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    try:
        _register_pdf_artifact(db, app_id, doc_id, tmp_path / "review" / "resume.pdf")
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            item_id = review.ensure_document_review(cur, doc_id)
            conn.commit()
        with db.connect(TEST_DSN) as conn:
            result = review.decide_item(conn, item_id, decision="revise", actor="integration-test")
            conn.commit()
        assert result["status"] == "needs_revision"
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            again = review.ensure_document_review(cur, doc_id)
            cur.execute(
                "SELECT status, payload_json FROM human_review_items WHERE id = %s",
                (item_id,),
            )
            status, payload = cur.fetchone()
            conn.commit()
        assert again == item_id
        assert status == "needs_revision"
        assert payload["human_revision_required"] is True
    finally:
        _cleanup(db, app_id)


def test_document_review_rejects_stale_job_description(db, monkeypatch, tmp_path):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    try:
        _register_pdf_artifact(db, app_id, doc_id, tmp_path / "review" / "resume.pdf")
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            item_id = review.ensure_document_review(cur, doc_id)
            cur.execute("UPDATE applications SET jd_hash = %s WHERE id = %s", ("e" * 64, app_id))
            conn.commit()
        with db.connect(TEST_DSN) as conn:
            with pytest.raises(review.ReviewError, match="job description changed"):
                review.decide_item(conn, item_id, decision="approve", actor="integration-test")
            conn.rollback()
    finally:
        _cleanup(db, app_id)


def test_document_review_rejects_mutated_pdf(db, monkeypatch, tmp_path):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    pdf = tmp_path / "review" / "resume.pdf"
    try:
        _register_pdf_artifact(db, app_id, doc_id, pdf)
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            item_id = review.ensure_document_review(cur, doc_id)
            conn.commit()
        pdf.write_bytes(pdf.read_bytes() + b"tampered")
        with db.connect(TEST_DSN) as conn:
            with pytest.raises(review.ReviewError, match="missing, changed, or unbound"):
                review.decide_item(conn, item_id, decision="approve", actor="integration-test")
            conn.rollback()
    finally:
        _cleanup(db, app_id)


def test_gmail_candidate_secret_context_uses_real_jsonb_adapter(db, monkeypatch):
    from services.auth import gmail_verification_v1 as gmail
    from datetime import datetime, timezone

    app_id, _doc_id = _fixture(db)
    monkeypatch.setenv("JOBOS_GMAIL_ACCOUNT", "candidate@example.com")
    candidate = {
        "message_id": f"integration-{uuid.uuid4()}",
        "sender": "recruiting@example.com",
        "subject": "Verification code",
        "received_at": datetime.now(timezone.utc),
        "kind": "numeric_code",
        "secret_sha256": "b" * 64,
        "secret_context": {"kind": "numeric_code", "digits": 6},
    }
    try:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            candidate_id = gmail.persist_candidate(cur, application_id=app_id, candidate=candidate)
            conn.commit()
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT secret_context_json FROM email_verification_candidates WHERE id=%s", (candidate_id,))
            stored = cur.fetchone()[0]
        assert stored == {"kind": "numeric_code", "digits": 6}
    finally:
        _cleanup(db, app_id)


def test_072_later_page_auth_transitions_exist(db):
    expected = {"needs_account_auth", "needs_email_verification", "needs_mfa", "needs_human_checkpoint"}
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT to_step FROM pipeline_transitions
                 WHERE from_step='application_ready'
                   AND to_step = ANY(%s);""",
            (list(expected),),
        )
        observed = {row[0] for row in cur.fetchall()}
    assert observed == expected


def test_privileged_expired_idempotency_slot_recreates_cleanly(db):
    from services.application_actions.action_request_v1 import create_privileged_request

    app_id, _doc_id = _fixture(db)
    payload = {"purpose": "integration-ttl"}
    try:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            first = create_privileged_request(
                cur,
                application_id=app_id,
                action_type="privileged_auth_manual_retry",
                payload=payload,
                summary="expired integration request",
                requested_by="integration-test",
                ttl_minutes=-1,
            )
            conn.commit()
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            second = create_privileged_request(
                cur,
                application_id=app_id,
                action_type="privileged_auth_manual_retry",
                payload=payload,
                summary="expired integration request",
                requested_by="integration-test",
                ttl_minutes=10,
            )
            conn.commit()
        assert second != first
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM approval_requests WHERE id=%s", (first,))
            assert cur.fetchone() == ("expired",)
            cur.execute("SELECT status FROM approval_requests WHERE id=%s", (second,))
            assert cur.fetchone() == ("pending",)
    finally:
        _cleanup(db, app_id)


def test_concurrent_privileged_materializers_reuse_one_active_request(db):
    from concurrent.futures import ThreadPoolExecutor
    from services.application_actions.action_request_v1 import create_privileged_request

    app_id, _doc_id = _fixture(db)
    payload = {"purpose": "integration-race"}

    def materialize(_index: int) -> str:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            request_id = create_privileged_request(
                cur,
                application_id=app_id,
                action_type="privileged_mfa_retry",
                payload=payload,
                summary="concurrent integration request",
                requested_by="integration-test",
                ttl_minutes=10,
            )
            conn.commit()
            return request_id

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(materialize, range(6)))
        assert len(set(ids)) == 1
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM approval_requests
                     WHERE application_id=%s AND type='privileged_mfa_retry'
                       AND status IN ('pending','approved','executing')""",
                (app_id,),
            )
            assert cur.fetchone() == (1,)
    finally:
        _cleanup(db, app_id)
