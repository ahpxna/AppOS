"""Durable idempotency/recovery fence for document-generation model calls.

A model response is not an externally durable business effect until the exact
``generated_documents`` row is committed. Therefore a crashed or transport-
uncertain generation must be retryable after a bounded lease; keeping it locked
forever converts idempotency into a liveness failure. Paid-provider uncertainty
is accounted independently by ``llm_cost_reservations`` and remains budgeted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


class DocumentAttemptError(RuntimeError):
    pass


class DocumentAttemptBusyError(DocumentAttemptError):
    """An identical attempt still owns a live recovery lease."""


@dataclass(frozen=True)
class DocumentAttempt:
    id: str
    idempotency_key: str
    completed_document_id: str | None = None
    attempt_count: int = 1


def canonical_key(manifest: Mapping[str, Any]) -> str:
    body = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def claim(cur, *, application_id: str, doc_type: str, request_kind: str,
          input_manifest: Mapping[str, Any], lease_seconds: int = 900) -> DocumentAttempt:
    """Claim/recover one exact generation identity before model I/O.

    * completed -> reuse the durable document;
    * running with a live lease -> do not race it;
    * stale running / retry-delay-expired uncertain / failed -> recover in place;
    * missing -> create one running attempt.
    """
    key = canonical_key(input_manifest)
    lease_s = max(30, min(int(lease_seconds), 7200))
    cur.execute(
        """SELECT id::text,status,generated_document_id::text,attempt_count,
                  CASE WHEN lease_expires_at IS NULL THEN true ELSE lease_expires_at <= now() END
             FROM document_generation_attempts
            WHERE application_id=%s AND doc_type=%s AND idempotency_key=%s
            FOR UPDATE;""",
        (application_id, doc_type, key),
    )
    existing = cur.fetchone()
    if existing:
        attempt_id, status, document_id = str(existing[0]), str(existing[1]), existing[2]
        attempt_count = int(existing[3] or 0)
        lease_expired = bool(existing[4])
        if status == "completed" and document_id:
            return DocumentAttempt(attempt_id, key, str(document_id), max(1, attempt_count))
        if status in {"running", "uncertain"} and not lease_expired:
            raise DocumentAttemptBusyError(
                "Identical document generation is temporarily unavailable while its recovery lease is live; retry later."
            )
        cur.execute(
            """UPDATE document_generation_attempts
                  SET status='running',error=NULL,updated_at=now(),finished_at=NULL,
                      attempt_count=GREATEST(attempt_count,0)+1,
                      lease_expires_at=now()+make_interval(secs => %s)
                WHERE id=%s
                RETURNING attempt_count;""",
            (lease_s, attempt_id),
        )
        return DocumentAttempt(attempt_id, key, None, int(cur.fetchone()[0]))

    cur.execute(
        """INSERT INTO document_generation_attempts(
               application_id,doc_type,idempotency_key,request_kind,input_manifest,status,
               attempt_count,lease_expires_at)
           VALUES (%s,%s,%s,%s,%s,'running',1,now()+make_interval(secs => %s))
           RETURNING id::text;""",
        (application_id, doc_type, key, request_kind,
         json.dumps(dict(input_manifest), default=str), lease_s),
    )
    return DocumentAttempt(str(cur.fetchone()[0]), key, None, 1)


def complete(cur, *, attempt_id: str, document_id: str) -> None:
    cur.execute(
        """UPDATE document_generation_attempts
              SET status='completed',generated_document_id=%s,updated_at=now(),finished_at=now(),
                  lease_expires_at=NULL,error=NULL
            WHERE id=%s AND status='running';""",
        (document_id, attempt_id),
    )
    if cur.rowcount == 1:
        return
    # Idempotent post-commit replay is okay only for the exact same document.
    cur.execute(
        """SELECT status,generated_document_id::text
             FROM document_generation_attempts WHERE id=%s;""",
        (attempt_id,),
    )
    row = cur.fetchone()
    if row and str(row[0]) == "completed" and str(row[1] or "") == str(document_id):
        return
    raise DocumentAttemptError("Document attempt changed before its durable result was recorded.")


def fail(cur, *, attempt_id: str, error: str, uncertain: bool = False,
         retry_delay_seconds: int = 60) -> None:
    """Close a failed call without creating an eternal non-replayable state."""
    delay = max(0, min(int(retry_delay_seconds), 3600))
    if uncertain:
        cur.execute(
            """UPDATE document_generation_attempts
                  SET status='uncertain',error=%s,updated_at=now(),finished_at=now(),
                      lease_expires_at=now()+make_interval(secs => %s)
                WHERE id=%s AND status='running';""",
            (error[:2000], delay, attempt_id),
        )
    else:
        cur.execute(
            """UPDATE document_generation_attempts
                  SET status='failed',error=%s,updated_at=now(),finished_at=now(),lease_expires_at=NULL
                WHERE id=%s AND status='running';""",
            (error[:2000], attempt_id),
        )
