#!/usr/bin/env python3
"""Durable worker for candidate-authored resume/cover-letter revision feedback.

The worker never treats human feedback as evidence. It asks the canonical
Document Generator to revise the exact reviewed draft, then runs the canonical
truth verifier. Review Hub materializes the new exact PDF on the next sync.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.common.config import database_dsn, load_repo_env
from services.control_plane.pipeline_state import DEFAULT_PIPELINE_STATE_STORE, PipelineStateError

TRANSIENT = (
    "Connection refused", "timed out", "Temporary failure", "Connection reset",
    "Ollama request failed", "URLError", "ConnectionError",
)


def _claim(conn, worker_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text,application_id::text,document_type,source_document_id::text,
                      source_review_item_id::text,source_sha256,feedback_text,attempt_count,
                      generated_document_id::text
                 FROM document_revision_requests
                WHERE status='pending'
                   OR (status='running' AND lease_expires_at < now())
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED LIMIT 1;"""
        )
        row = cur.fetchone()
        if not row:
            conn.rollback(); return None
        cur.execute(
            """UPDATE document_revision_requests
                  SET status='running',claimed_by=%s,lease_expires_at=now()+interval '35 minutes',
                      attempt_count=attempt_count+1,error_message=NULL,updated_at=now()
                WHERE id=%s;""",
            (worker_id, row[0]),
        )
    conn.commit()
    return row


def _run(argv: list[str], *, input_text: str | None = None) -> tuple[bool, str, bool]:
    try:
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, input=input_text, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        detail = f"document subprocess timed out after {int(exc.timeout or 1800)} seconds"
        return False, detail, True
    except OSError as exc:
        return False, f"document subprocess could not start: {exc}", True
    out = ((proc.stdout or "") + (proc.stderr or ""))[-12000:]
    return proc.returncode == 0, out, any(marker in out for marker in TRANSIENT)


def _generated_document_id(output: str) -> str | None:
    match = re.search(r"generated_document_id:\s*([0-9a-fA-F-]{36})", output or "")
    return str(match.group(1)) if match else None


def _renew_lease(conn, request_id: str, worker_id: str) -> bool:
    """Renew ownership before the next bounded external subprocess."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE document_revision_requests
                  SET lease_expires_at=now()+interval '35 minutes',updated_at=now()
                WHERE id=%s AND status='running' AND claimed_by=%s
                RETURNING id;""",
            (request_id, worker_id),
        )
        owned = bool(cur.fetchone())
    conn.commit()
    return owned




def _application_terminal(conn, application_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM applications WHERE id=%s;", (application_id,))
        row = cur.fetchone()
    conn.rollback()
    status = str(row[0] or "") if row else "missing"
    return status if status in {"submitted", "abandoned", "missing"} else None


def _cancel(conn, request_id: str, review_item_id: str, reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE document_revision_requests
                  SET status='cancelled',claimed_by=NULL,lease_expires_at=NULL,error_message=%s,
                      finished_at=now(),updated_at=now()
                WHERE id=%s AND status IN ('pending','running');""",
            (reason[-3000:], request_id),
        )
        cur.execute(
            """UPDATE human_review_items
                  SET status='resolved',decision_note=%s,decided_at=now(),updated_at=now()
                WHERE id=%s AND item_type='document_review'
                  AND status IN ('pending','needs_revision');""",
            (reason[-1500:], review_item_id),
        )
    conn.commit()

def _finish_failure(conn, request_id: str, review_item_id: str, error: str, *, transient: bool, attempts: int) -> None:
    with conn.cursor() as cur:
        if transient and attempts < 3:
            cur.execute(
                """UPDATE document_revision_requests
                      SET status='pending',claimed_by=NULL,lease_expires_at=NULL,error_message=%s,updated_at=now()
                    WHERE id=%s;""", (error[-3000:], request_id)
            )
        else:
            cur.execute(
                """UPDATE document_revision_requests
                      SET status='failed',claimed_by=NULL,lease_expires_at=NULL,error_message=%s,
                          finished_at=now(),updated_at=now()
                    WHERE id=%s;""", (error[-3000:], request_id)
            )
            cur.execute(
                """UPDATE human_review_items
                      SET status='needs_revision',
                          summary_text='Document revision failed safely. Edit the feedback and try again.',
                          payload_json=payload_json || %s,updated_at=now()
                    WHERE id=%s AND item_type='document_review';""",
                (Jsonb({"document_revision_error": error[-1500:]}), review_item_id),
            )
    conn.commit()


def process_one(conn, worker_id: str) -> bool:
    row = _claim(conn, worker_id)
    if not row:
        return False
    request_id, app_id, doc_type, source_doc_id, review_item_id, source_sha, feedback, old_attempts, checkpoint_doc_id = row
    attempts = int(old_attempts or 0) + 1
    with conn.cursor() as cur:
        cur.execute(
            """SELECT gd.content,gd.doc_type,gd.application_id::text,gd.source_jd_hash,a.jd_hash,
                      h.source_sha256,h.status,a.status
                 FROM generated_documents gd
                 JOIN applications a ON a.id=gd.application_id
                 JOIN human_review_items h ON h.id=%s
                WHERE gd.id=%s;""",
            (review_item_id, source_doc_id),
        )
        source = cur.fetchone()
    conn.rollback()
    import hashlib
    if source and str(source[7] or '') in {'submitted','abandoned'}:
        _cancel(conn, request_id, review_item_id,
                f"Document revision cancelled because application is {str(source[7])}.")
        return True
    if (not source or str(source[1]) != str(doc_type) or str(source[2]) != str(app_id)
            or str(source[5] or "") != str(source_sha)
            or hashlib.sha256(str(source[0] or "").encode()).hexdigest() != str(source_sha)
            or not source[3] or str(source[3]) != str(source[4] or "")):
        _finish_failure(conn, request_id, review_item_id,
                        "Exact reviewed source/JD binding changed; submit feedback on the newest document card.",
                        transient=False, attempts=attempts)
        return True

    new_doc_id = str(checkpoint_doc_id or "").strip()
    if new_doc_id:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id::text FROM generated_documents
                     WHERE id=%s AND application_id=%s AND doc_type=%s AND id<>%s;""",
                (new_doc_id, app_id, doc_type, source_doc_id),
            )
            generated = cur.fetchone()
        conn.rollback()
        if not generated:
            _finish_failure(conn, request_id, review_item_id,
                            "checkpointed generated document no longer matches the exact revision binding",
                            transient=False, attempts=attempts)
            return True
    else:
        generator = [
            sys.executable, str(ROOT / "services" / "document-generation" / "generate_documents_v1.py"),
            "--application-id", app_id, "--doc-type", doc_type, "--apply",
            "--revision-source-document-id", source_doc_id, "--revision-feedback-stdin",
        ]
        if not _renew_lease(conn, request_id, worker_id):
            return True
        ok, out, transient = _run(generator, input_text=str(feedback))
        if not ok:
            _finish_failure(conn, request_id, review_item_id, out or "document generator failed",
                            transient=transient, attempts=attempts)
            return True

        # Bind to the exact document id emitted by this generator invocation.
        # Selecting "latest" is racy with orchestrator/manual regeneration and can
        # attach the human feedback request to somebody else's concurrently-created
        # draft.
        emitted_document_id = _generated_document_id(out)
        if not emitted_document_id:
            _finish_failure(conn, request_id, review_item_id, "generator completed without an exact generated_document_id",
                            transient=False, attempts=attempts)
            return True
        new_doc_id = emitted_document_id
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id::text FROM generated_documents
                     WHERE id=%s AND application_id=%s AND doc_type=%s AND id<>%s;""",
                (new_doc_id, app_id, doc_type, source_doc_id),
            )
            generated = cur.fetchone()
            if generated:
                cur.execute(
                    """UPDATE document_revision_requests
                          SET generated_document_id=%s,updated_at=now()
                        WHERE id=%s AND status='running' AND claimed_by=%s;""",
                    (new_doc_id, request_id, worker_id),
                )
        conn.commit()
        if not generated:
            _finish_failure(conn, request_id, review_item_id,
                            "generator emitted a document id outside the exact application/document binding",
                            transient=False, attempts=attempts)
            return True

    terminal_status = _application_terminal(conn, app_id)
    if terminal_status:
        _cancel(conn, request_id, review_item_id,
                f"Document revision cancelled because application is {terminal_status}.")
        return True

    with conn.cursor() as cur:
        cur.execute("SELECT qa_status FROM generated_documents WHERE id=%s;", (new_doc_id,))
        qa = cur.fetchone()
    conn.rollback()
    if not qa or str(qa[0] or "") not in {"pass", "revise", "fail"}:
        verifier = [
            sys.executable, str(ROOT / "services" / "document-generation" / "verify_document_truth_v1.py"),
            "--document-id", new_doc_id, "--apply",
        ]
        if not _renew_lease(conn, request_id, worker_id):
            return True
        vok, vout, vtransient = _run(verifier)
        if not vok:
            _finish_failure(conn, request_id, review_item_id, vout or "truth verifier failed",
                            transient=vtransient, attempts=attempts)
            return True
        with conn.cursor() as cur:
            cur.execute("SELECT qa_status FROM generated_documents WHERE id=%s;", (new_doc_id,))
            qa = cur.fetchone()
        conn.rollback()

    if not qa or str(qa[0] or "") not in {"pass", "revise", "fail"}:
        _finish_failure(conn, request_id, review_item_id,
                        "truth verifier returned success without a persisted terminal QA status",
                        transient=False, attempts=attempts)
        return True

    terminal_status = _application_terminal(conn, app_id)
    if terminal_status:
        _cancel(conn, request_id, review_item_id,
                f"Document revision cancelled because application is {terminal_status}.")
        return True

    with conn.cursor() as cur:
        if str(qa[0] if qa else "") == "pass" and doc_type == "resume":
            cur.execute("SELECT current_step FROM applications WHERE id=%s FOR UPDATE;", (app_id,))
            app_step = cur.fetchone()
            if app_step and str(app_step[0]) == "docs_failed_qa":
                try:
                    DEFAULT_PIPELINE_STATE_STORE.transition(
                        cur, application_id=app_id, expected_from="docs_failed_qa",
                        to="docs_verified", actor="document_revision_worker",
                        reason="Candidate-requested revision passed truth QA.",
                        detail={"generated_document_id": new_doc_id,
                                "revision_request_id": request_id},
                        require_automated=True,
                    )
                except PipelineStateError as exc:
                    conn.rollback()
                    _finish_failure(conn, request_id, review_item_id,
                                    f"document QA recovery transition failed: {exc}",
                                    transient=False, attempts=attempts)
                    return True
        cur.execute(
            """UPDATE document_revision_requests
                  SET status='completed',generated_document_id=%s,claimed_by=NULL,lease_expires_at=NULL,
                      error_message=NULL,finished_at=now(),updated_at=now()
                WHERE id=%s AND status='running' AND claimed_by=%s;""", (new_doc_id, request_id, worker_id)
        )
        if cur.rowcount != 1:
            conn.rollback()
            return True
        cur.execute(
            """UPDATE human_review_items
                  SET status='resolved',decided_at=now(),
                      decision_note='Superseded by a candidate-requested regenerated draft.',updated_at=now()
                WHERE id=%s AND item_type='document_review';""", (review_item_id,)
        )
        cur.execute(
            """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
               VALUES (%s,'document_revision_completed','document_revision_worker',%s);""",
            (app_id, Jsonb({"revision_request_id": request_id, "source_document_id": source_doc_id,
                            "generated_document_id": new_doc_id, "doc_type": doc_type,
                            "qa_status": str(qa[0] if qa else "")})),
        )
    conn.commit()
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--poll-seconds", type=float, default=5.0)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    load_repo_env()
    worker_id = f"doc-revision:{os.getpid()}"
    while True:
        with psycopg.connect(database_dsn(), autocommit=False) as conn:
            did = process_one(conn, worker_id)
        if args.once:
            return 0
        if not did:
            time.sleep(max(1.0, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
