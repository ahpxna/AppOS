from __future__ import annotations
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
    with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO applications(id, source, company, job_title, current_step, status, created_at, updated_at)
                       VALUES (%s, 'test', 'Review Fixture Co', 'Security Engineer', 'docs_verified', 'active', now(), now())""", (app_id,))
        cur.execute("""INSERT INTO generated_documents(id, application_id, doc_type, version, content, qa_status, approved, created_at)
                       VALUES (%s, %s, 'resume', 1, 'truth checked resume', 'pass', false, now())""", (doc_id, app_id))
        conn.commit()
    return app_id, doc_id


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


def test_document_review_approves_current_qa_passed_content(db, monkeypatch):
    from services.review import review_service_v1 as review
    monkeypatch.setenv("JOBOS_REVIEW_AUTO_RENDER_PDFS", "false")
    app_id, doc_id = _fixture(db)
    try:
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            item_id = review.ensure_document_review(cur, doc_id); conn.commit()
        with db.connect(TEST_DSN) as conn:
            result = review.decide_item(conn, item_id, decision="approve", actor="integration-test"); conn.commit()
        assert result["status"] == "approved"
        with db.connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT approved FROM generated_documents WHERE id = %s", (doc_id,)); assert cur.fetchone()[0] is True
    finally:
        _cleanup(db, app_id)
