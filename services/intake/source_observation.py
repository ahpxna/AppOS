"""Durable source-observation boundary for mutable job postings.

A public job source is mutable while a JobOS application becomes an evidence
snapshot.  Only applications still at ``intake`` may be refreshed in place.
Once fit analysis or any later stage has started, incoming source changes are
stored append-only in ``job_posting_source_revisions`` and surfaced as an audit
event; they never rewrite the JD/hash/URL that downstream documents and
approvals were bound to.

Revision identity deliberately covers the *whole normalized posting surface*,
not just JD text.  Employers commonly change title, location, work mode, URL,
or requisition id without editing the description; those changes must remain
observable without mutating downstream evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from services.ats.contracts import canonical_job_url, normalize_work_mode
from services.intake.posting_identity import PostingIdentity, find_existing_application


@dataclass(frozen=True)
class SourceObservationResult:
    application_id: str
    current_step: str
    disposition: str  # promoted | unchanged | changed_downstream
    revision_id: str | None


def source_content_sha256(*, jd_hash: str, company: str | None, job_title: str | None,
                          location: str | None, work_mode: str | None,
                          canonical_url: str | None, source_job_id: str | None) -> str:
    """Hash the normalized source fields whose change matters to provenance."""
    fields = (
        canonical_job_url(canonical_url),
        " ".join(str(company or "").split()),
        str(jd_hash or "").strip().casefold(),
        " ".join(str(job_title or "").split()),
        " ".join(str(location or "").split()),
        str(source_job_id or "").strip(),
        normalize_work_mode(work_mode).value,
    )
    # Unit Separator is valid PostgreSQL text and lets migration 085 backfill
    # byte-for-byte the same digest without depending on JSON serializer layout.
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def find_and_observe_existing(
    cur: Any,
    *,
    identity: PostingIdentity,
    source_name: str,
    jd_text: str,
    company: str | None,
    job_title: str | None,
    location: str | None = None,
    work_mode: str | None = None,
    source_job_id: str | None = None,
    ats_company_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, str, str, str, str] | None, SourceObservationResult | None]:
    """Canonical duplicate boundary used by every ordinary intake entrypoint.

    Identity lookup and source observation must stay adjacent.  Returning early
    after finding a duplicate loses legitimate employer edits; callers should
    never re-implement that pattern themselves.
    """
    existing = find_existing_application(
        cur, identity, ats_company_id=ats_company_id, source_job_id=source_job_id
    )
    if not existing:
        return None, None
    observed = observe_existing_posting(
        cur,
        application_id=existing[0],
        source_name=source_name,
        source_job_id=source_job_id,
        company=company,
        job_title=job_title,
        job_url=identity.canonical_url,
        jd_text=jd_text,
        jd_hash=identity.jd_hash,
        location=location,
        work_mode=work_mode,
        metadata=metadata,
    )
    return existing, observed


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
    """Record one source observation without mutating a downstream snapshot."""
    text = str(jd_text or "").strip()
    digest = str(jd_hash or "").strip().lower()
    if not text or len(digest) != 64:
        raise ValueError("source observation requires non-empty JD text and a SHA-256 hash")

    canonical_url = canonical_job_url(job_url)
    normalized_mode = normalize_work_mode(work_mode).value
    observed_content_hash = source_content_sha256(
        jd_hash=digest,
        company=company,
        job_title=job_title,
        location=location,
        work_mode=normalized_mode,
        canonical_url=canonical_url,
        source_job_id=source_job_id,
    )

    # Lock the application so an intake refresh cannot race a transition into
    # downstream evidence processing.
    cur.execute(
        """SELECT coalesce(current_step,''), coalesce(jd_hash,''),
                  coalesce(company,''), coalesce(job_title,''), coalesce(location,''),
                  coalesce(work_mode,'unknown'), coalesce(job_url,''), coalesce(source_job_id,'')
             FROM applications WHERE id=%s FOR UPDATE;""",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"application not found: {application_id}")
    current_step = str(row[0] or "")
    existing_hash = str(row[1] or "")
    current_content_hash = source_content_sha256(
        jd_hash=existing_hash,
        company=str(row[2] or ""),
        job_title=str(row[3] or ""),
        location=str(row[4] or ""),
        work_mode=str(row[5] or "unknown"),
        canonical_url=str(row[6] or ""),
        source_job_id=str(row[7] or ""),
    )

    source_key = str(source_name or "unknown")
    cur.execute(
        """SELECT id::text FROM job_posting_source_revisions
             WHERE application_id=%s AND source_name=%s AND source_content_sha256=%s;""",
        (application_id, source_key, observed_content_hash),
    )
    prior_revision = cur.fetchone()
    cur.execute(
        """INSERT INTO job_posting_source_revisions(
               application_id, source_name, source_job_id, canonical_url,
               company, job_title, location, work_mode, jd_hash, jd_text,
               source_content_sha256, metadata_json, promoted_at)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                     CASE WHEN %s='intake' THEN now() ELSE NULL END)
             ON CONFLICT (application_id, source_name, source_content_sha256)
             DO UPDATE SET observed_at=now(),
                           source_job_id=coalesce(EXCLUDED.source_job_id, job_posting_source_revisions.source_job_id),
                           canonical_url=coalesce(nullif(EXCLUDED.canonical_url,''), job_posting_source_revisions.canonical_url),
                           company=coalesce(nullif(EXCLUDED.company,''), job_posting_source_revisions.company),
                           job_title=coalesce(nullif(EXCLUDED.job_title,''), job_posting_source_revisions.job_title),
                           location=coalesce(nullif(EXCLUDED.location,''), job_posting_source_revisions.location),
                           work_mode=CASE WHEN EXCLUDED.work_mode='unknown' THEN job_posting_source_revisions.work_mode ELSE EXCLUDED.work_mode END,
                           metadata_json=job_posting_source_revisions.metadata_json || EXCLUDED.metadata_json
             RETURNING id::text;""",
        (
            application_id,
            source_key,
            str(source_job_id or "") or None,
            canonical_url,
            str(company or ""),
            str(job_title or ""),
            str(location or ""),
            normalized_mode,
            digest,
            text,
            observed_content_hash,
            Jsonb(dict(metadata or {})),
            current_step,
        ),
    )
    revision_id = str(cur.fetchone()[0])
    inserted = prior_revision is None

    # Liveness/visibility fields are safe source metadata at every stage.
    cur.execute(
        """UPDATE applications
              SET last_seen_at=now(), stale_at=NULL, closed_at=NULL, updated_at=now()
            WHERE id=%s;""",
        (application_id,),
    )

    if current_step == "intake":
        changed = current_content_hash != observed_content_hash
        cur.execute(
            """UPDATE applications
                  SET company=coalesce(nullif(%s,''),company),
                      job_title=coalesce(nullif(%s,''),job_title),
                      job_url=coalesce(nullif(%s,''),job_url),
                      jd_text=%s, jd_hash=%s,
                      location=coalesce(nullif(%s,''),location),
                      work_mode=CASE WHEN %s='unknown' THEN work_mode ELSE %s END,
                      source_job_id=coalesce(nullif(%s,''),source_job_id),
                      last_content_change_at=CASE WHEN %s THEN now() ELSE last_content_change_at END,
                      updated_at=now()
                WHERE id=%s;""",
            (
                str(company or ""),
                str(job_title or ""),
                canonical_url,
                text,
                digest,
                str(location or ""),
                normalized_mode,
                normalized_mode,
                str(source_job_id or ""),
                changed,
                application_id,
            ),
        )
        cur.execute(
            "UPDATE job_posting_source_revisions SET promoted_at=coalesce(promoted_at,now()) WHERE id=%s;",
            (revision_id,),
        )
        return SourceObservationResult(application_id, current_step, "promoted", revision_id)

    if current_content_hash == observed_content_hash:
        return SourceObservationResult(application_id, current_step, "unchanged", revision_id)

    if inserted:
        cur.execute(
            """INSERT INTO application_events(application_id,event_type,event_source,event_payload)
                 VALUES (%s,'job_source_revision_detected',%s,%s);""",
            (
                application_id,
                source_key,
                Jsonb(
                    {
                        "revision_id": revision_id,
                        "current_step": current_step,
                        "snapshot_jd_hash": existing_hash,
                        "observed_jd_hash": digest,
                        "observed_source_content_sha256": observed_content_hash,
                        "canonical_url": canonical_url,
                        "source_job_id": str(source_job_id or "") or None,
                        "changed_fields_may_include": [
                            "jd_text",
                            "company",
                            "job_title",
                            "location",
                            "work_mode",
                            "canonical_url",
                            "source_job_id",
                        ],
                        "action": "snapshot_preserved",
                    }
                ),
            ),
        )
    return SourceObservationResult(application_id, current_step, "changed_downstream", revision_id)
