"""Durable idempotency fence for document-generation side effects."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


class DocumentAttemptError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentAttempt:
    id: str
    idempotency_key: str
    completed_document_id: str | None = None


def canonical_key(manifest: Mapping[str, Any]) -> str:
    body = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def claim(cur, *, application_id: str, doc_type: str, request_kind: str,
          input_manifest: Mapping[str, Any]) -> DocumentAttempt:
    """Create/claim an exact attempt before model I/O.

    A completed attempt reuses its durable document.  A live/uncertain attempt
    is deliberately not replayed: after a process crash the model outcome is
    unknown, and a second generation could create duplicate paid cost or a
    conflicting artifact.  It requires explicit recovery rather than hope.
    """
    key = canonical_key(input_manifest)
    cur.execute(
        """SELECT id::text,status,generated_document_id::text
             FROM document_generation_attempts
            WHERE application_id=%s AND doc_type=%s AND idempotency_key=%s
            FOR UPDATE;""",
        (application_id, doc_type, key),
    )
    existing = cur.fetchone()
    if existing:
        attempt_id, status, document_id = str(existing[0]), str(existing[1]), existing[2]
        if status == "completed" and document_id:
            return DocumentAttempt(attempt_id, key, str(document_id))
        if status in {"running", "uncertain"}:
            raise DocumentAttemptError(
                "An identical document-generation attempt has an uncertain external outcome; "
                "inspect/recover it instead of replaying model generation."
            )
        cur.execute(
            """UPDATE document_generation_attempts
                  SET status='running',error=NULL,updated_at=now(),finished_at=NULL
                WHERE id=%s;""",
            (attempt_id,),
        )
        return DocumentAttempt(attempt_id, key)
    cur.execute(
        """INSERT INTO document_generation_attempts(
               application_id,doc_type,idempotency_key,request_kind,input_manifest,status)
           VALUES (%s,%s,%s,%s,%s,'running') RETURNING id::text;""",
        (application_id, doc_type, key, request_kind, json.dumps(dict(input_manifest), default=str)),
    )
    return DocumentAttempt(str(cur.fetchone()[0]), key)


def complete(cur, *, attempt_id: str, document_id: str) -> None:
    cur.execute(
        """UPDATE document_generation_attempts
              SET status='completed',generated_document_id=%s,updated_at=now(),finished_at=now()
            WHERE id=%s AND status='running';""",
        (document_id, attempt_id),
    )
    if cur.rowcount != 1:
        raise DocumentAttemptError("Document attempt changed before its durable result was recorded.")


def fail(cur, *, attempt_id: str, error: str, uncertain: bool = False) -> None:
    cur.execute(
        """UPDATE document_generation_attempts
              SET status=%s,error=%s,updated_at=now(),finished_at=now()
            WHERE id=%s AND status='running';""",
        ("uncertain" if uncertain else "failed", error[:2000], attempt_id),
    )
