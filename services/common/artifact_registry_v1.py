"""Durable artifact/template/render provenance around pure renderers.

Large bytes remain on disk/object storage. PostgreSQL owns identity, hashes,
provenance and render lifecycle so a restart can explain exactly which bytes
were produced by which immutable input/template revision.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import uuid

import psycopg
from psycopg.types.json import Jsonb

from services.common.config import database_dsn


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def register_artifact(cur, *, application_id: str | None, artifact_kind: str,
                      path: Path, mime_type: str | None,
                      provenance: dict[str, Any] | None = None) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"artifact bytes do not exist: {resolved}")
    digest = sha256_file(resolved)
    cur.execute(
        """INSERT INTO artifacts(application_id,artifact_kind,storage_backend,storage_key,filename,
                                  mime_type,size_bytes,sha256,provenance_json,status)
           VALUES (%s,%s,'filesystem',%s,%s,%s,%s,%s,%s,'available')
           ON CONFLICT (storage_backend,storage_key,sha256) DO UPDATE SET
             status='available',mime_type=coalesce(EXCLUDED.mime_type,artifacts.mime_type),
             size_bytes=EXCLUDED.size_bytes,provenance_json=artifacts.provenance_json || EXCLUDED.provenance_json
           RETURNING id::text;""",
        (application_id, artifact_kind, str(resolved), resolved.name, mime_type,
         resolved.stat().st_size, digest, Jsonb(provenance or {})),
    )
    return str(cur.fetchone()[0])


def _ensure_template_revision(cur, template: Path | None) -> str | None:
    if template is None:
        return None
    resolved = template.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"render template missing: {resolved}")
    digest = sha256_file(resolved)
    key = resolved.name
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (key,))
    cur.execute("SELECT id::text FROM document_template_revisions WHERE template_key=%s AND sha256=%s;",
                (key, digest))
    row = cur.fetchone()
    if row:
        return str(row[0])
    cur.execute("SELECT version FROM document_template_revisions WHERE template_key=%s ORDER BY version DESC LIMIT 1 FOR UPDATE;",
                (key,))
    row = cur.fetchone()
    version = int(row[0]) + 1 if row else 1
    cur.execute(
        """INSERT INTO document_template_revisions(template_key,version,file_path,sha256,contract_version,status)
           VALUES (%s,%s,%s,%s,'canonical-resume-v1','active') RETURNING id::text;""",
        (key, version, str(resolved), digest),
    )
    return str(cur.fetchone()[0])


@dataclass(frozen=True)
class RenderClaim:
    run_id: str
    claim_token: str


def begin_render_run(*, document_id: str, input_manifest: dict[str, Any],
                     template: Path | None = None, claimed_by: str = "jobos-renderer") -> RenderClaim:
    """Claim one immutable render identity before filesystem output I/O.

    The row is locked before takeover. A live lease owned by another renderer
    fails closed; a stale/terminal identity may be deterministically rendered
    again under a fresh claim token without creating a second logical run.
    """
    input_sha = sha256_json(input_manifest)
    lease_seconds = max(60, int(os.getenv("JOBOS_RENDER_LEASE_SECONDS", "900")))
    claim_token = str(uuid.uuid4())
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        template_id = _ensure_template_revision(cur, template)
        idem = sha256_json({"document_id": document_id, "input_sha256": input_sha,
                            "template_revision_id": template_id, "renderer_contract": "v1"})
        cur.execute(
            """SELECT id::text,status,lease_expires_at > now()
                 FROM document_render_runs WHERE idempotency_key=%s FOR UPDATE;""",
            (idem,),
        )
        row = cur.fetchone()
        if row:
            run_id, status, lease_live = str(row[0]), str(row[1]), bool(row[2])
            if status == "running" and lease_live:
                raise RuntimeError(f"render identity {run_id} is already leased by another renderer")
            # Preserve the abandoned attempt as an explicit fact before takeover.
            cur.execute(
                """UPDATE document_render_attempts
                      SET status='superseded',finished_at=coalesce(finished_at,now()),
                          error_message=coalesce(error_message,'Lease expired or logical render was retried by a new owner.')
                    WHERE render_run_id=%s AND status='running';""", (run_id,),
            )
            cur.execute(
                """UPDATE document_render_runs
                      SET status='running',claimed_by=%s,claim_token=%s::uuid,
                          attempt_count=attempt_count+1,
                          lease_expires_at=now()+make_interval(secs => %s),
                          started_at=now(),finished_at=NULL,error_message=NULL
                    WHERE id=%s
                    RETURNING attempt_count;""",
                (claimed_by, claim_token, lease_seconds, run_id),
            )
            attempt_no = int(cur.fetchone()[0])
        else:
            cur.execute(
                """INSERT INTO document_render_runs(
                       generated_document_id,template_revision_id,input_sha256,idempotency_key,status,
                       claimed_by,claim_token,attempt_count,lease_expires_at,started_at)
                   VALUES (%s,%s,%s,%s,'running',%s,%s::uuid,1,
                           now()+make_interval(secs => %s),now())
                   RETURNING id::text;""",
                (document_id, template_id, input_sha, idem, claimed_by, claim_token, lease_seconds),
            )
            run_id = str(cur.fetchone()[0])
            attempt_no = 1
        cur.execute(
            """INSERT INTO document_render_attempts(
                   render_run_id,attempt_no,claim_token,claimed_by,status,lease_expires_at)
               VALUES (%s,%s,%s::uuid,%s,'running',now()+make_interval(secs => %s));""",
            (run_id, attempt_no, claim_token, claimed_by, lease_seconds),
        )
        conn.commit()
        return RenderClaim(run_id=run_id, claim_token=claim_token)

def finish_render_run(claim: RenderClaim, *, docx_artifact_id: str | None = None,
                      pdf_artifact_id: str | None = None) -> None:
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE document_render_runs
                  SET status='completed',docx_artifact_id=%s,pdf_artifact_id=%s,
                      finished_at=now(),lease_expires_at=NULL,error_message=NULL
                WHERE id=%s AND claim_token=%s::uuid AND status='running';""",
            (docx_artifact_id, pdf_artifact_id, claim.run_id, claim.claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("render lease changed before completion; refusing stale completion")
        cur.execute(
            """UPDATE document_render_attempts SET status='completed',finished_at=now(),lease_expires_at=NULL
                WHERE render_run_id=%s AND claim_token=%s::uuid AND status='running';""",
            (claim.run_id, claim.claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("render attempt journal changed before completion")
        conn.commit()


def fail_render_run(claim: RenderClaim, exc: BaseException, *, uncertain: bool = False) -> None:
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        status = "uncertain" if uncertain else "failed"
        cur.execute(
            """UPDATE document_render_runs SET status=%s,error_message=%s,finished_at=now(),lease_expires_at=NULL
                WHERE id=%s AND claim_token=%s::uuid AND status='running';""",
            (status, str(exc)[:2000], claim.run_id, claim.claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("render lease changed before failure could be recorded")
        cur.execute(
            """UPDATE document_render_attempts SET status=%s,error_message=%s,finished_at=now(),lease_expires_at=NULL
                WHERE render_run_id=%s AND claim_token=%s::uuid AND status='running';""",
            (status, str(exc)[:2000], claim.run_id, claim.claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("render attempt journal changed before failure could be recorded")
        conn.commit()
