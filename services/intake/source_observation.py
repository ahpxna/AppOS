"""Durable source-observation boundary for discovered job postings.

A job board is mutable while a JobOS application becomes an evidence snapshot.
Only applications that are still at ``intake`` may be refreshed in place. Once
fit analysis or any later stage has started, incoming source changes are stored
append-only in ``job_posting_source_revisions`` and surfaced as an audit event;
they never rewrite the JD/hash/URL that downstream documents and approvals were
bound to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from services.ats.contracts import canonical_job_url, normalize_work_mode


@dataclass(frozen=True)
class SourceObservationResult:
    application_id: str
    current_step: str
    disposition: str  # promoted | unchanged | changed_downstream
    revision_id: str | None


def observe_existing_posting(
    cur: Any,
    *,
    application_id: str,
    source_name: str,
    jd_text: str,
    jd_hash: str,
    job_url: str | None,
    company: str | None,
    job_title: str | None,
    location: str | None = None,
    work_mode: str | None = None,
    source_job_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceObservationResult:
    """Record one source observation without mutating a downstream snapshot.

    The application row is locked so a concurrent orchestrator transition cannot
    race an intake refresh. A new revision event is emitted only for a newly
    observed content hash, making repeated polling idempotent.
    """
    text = str(jd_text or "").strip()
    digest = str(jd_hash or "").strip().lower()
    if not text or len(digest) != 64:
        raise ValueError("source observation requires non-empty JD text and a SHA-256 hash")

    canonical_url = canonical_job_url(job_url)
    cur.execute(
        """SELECT coalesce(current_step,''), coalesce(jd_hash,'')
             FROM applications WHERE id=%s FOR UPDATE;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"application not found: {application_id}")
    current_step, existing_hash = str(row[0] or ""), str(row[1] or "")

    source_key = str(source_name or "unknown")
    cur.execute(
        """SELECT id::text FROM job_posting_source_revisions
             WHERE application_id=%s AND source_name=%s AND jd_hash=%s;""",
        (application_id, source_key, digest),
    )
    prior_revision = cur.fetchone()
    cur.execute(
        """INSERT INTO job_posting_source_revisions(
               application_id, source_name, source_job_id, canonical_url,
               company, job_title, location, work_mode, jd_hash, jd_text,
               metadata_json, promoted_at)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                     CASE WHEN %s='intake' THEN now() ELSE NULL END)
             ON CONFLICT (application_id, source_name, jd_hash)
             DO UPDATE SET observed_at=now(),
                           source_job_id=coalesce(EXCLUDED.source_job_id, job_posting_source_revisions.source_job_id),
                           canonical_url=coalesce(nullif(EXCLUDED.canonical_url,''), job_posting_source_revisions.canonical_url),
                           metadata_json=job_posting_source_revisions.metadata_json || EXCLUDED.metadata_json
             RETURNING id::text;""",
        (
            application_id, source_key,
            str(source_job_id or "") or None,
            canonical_url,
            str(company or ""),
            str(job_title or ""),
            str(location or ""),
            normalize_work_mode(work_mode).value,
            digest,
            text,
            Jsonb(dict(metadata or {})),
            current_step,
        ),
    )
    revision_id = cur.fetchone()[0]
    inserted = prior_revision is None

    # Liveness/visibility fields are safe source metadata at every stage.
    cur.execute(
        """UPDATE applications
              SET last_seen_at=now(), stale_at=NULL, closed_at=NULL, updated_at=now()
            WHERE id=%s;""",
        (application_id,),
    )

    if current_step == "intake":
        changed = existing_hash != digest
        normalized_mode = normalize_work_mode(work_mode).value
        cur.execute(
            """UPDATE applications
                  SET company=coalesce(nullif(%s,''),company),
                      job_title=coalesce(nullif(%s,''),job_title),
                      job_url=coalesce(nullif(%s,''),job_url),
                      jd_text=%s, jd_hash=%s,
                      location=coalesce(nullif(%s,''),location),
                      work_mode=CASE WHEN %s='unknown' THEN work_mode ELSE %s END,
                      last_content_change_at=CASE WHEN %s THEN now() ELSE last_content_change_at END,
                      updated_at=now()
                WHERE id=%s;""",
            (
                str(company or ""), str(job_title or ""), canonical_url,
                text, digest, str(location or ""), normalized_mode, normalized_mode,
                changed, application_id,
            ),
        )
        cur.execute(
            "UPDATE job_posting_source_revisions SET promoted_at=coalesce(promoted_at,now()) WHERE id=%s;",
            (revision_id,),
        )
        return SourceObservationResult(application_id, current_step, "promoted", str(revision_id))

    if existing_hash == digest:
        return SourceObservationResult(application_id, current_step, "unchanged", str(revision_id))

    if inserted:
        cur.execute(
            """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                 VALUES (%s,'job_source_revision_detected',%s,%s);""",
            (
                application_id,
                str(source_name or "source_observation"),
                Jsonb({
                    "revision_id": str(revision_id),
                    "current_step": current_step,
                    "snapshot_jd_hash": existing_hash,
                    "observed_jd_hash": digest,
                    "canonical_url": canonical_url,
                    "source_job_id": str(source_job_id or "") or None,
                    "action": "snapshot_preserved",
                }),
            ),
        )
    return SourceObservationResult(application_id, current_step, "changed_downstream", str(revision_id))
