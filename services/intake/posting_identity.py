"""Canonical posting identity shared by every JobOS intake entrypoint.

Posting content is evidence, not identity.  Prefer a source-stable ATS key when
available, otherwise an exact canonical job URL, and only fall back to the JD
hash plus employer/title when no URL exists.  This module does not write DB
state; callers retain their own event/source-specific persistence semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from services.ats.contracts import canonical_job_url
from services.ats.registry import detect_ats_platform, normalize_ats_key


def _norm_label(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


@dataclass(frozen=True)
class PostingIdentity:
    canonical_url: str
    jd_hash: str
    ats_type: str
    company_key: str
    title_key: str


def build_posting_identity(*, company: str | None, job_title: str | None,
                           jd_text: str, job_url: str | None,
                           ats_hint: str | None = None) -> PostingIdentity:
    text = str(jd_text or "").strip()
    canonical_url = canonical_job_url(job_url)
    detected = detect_ats_platform(canonical_url) if canonical_url else "custom"
    hinted = normalize_ats_key(ats_hint) if ats_hint else "custom"
    # URL evidence wins over a generic/unknown hint. An explicit known hint is
    # retained when the employer uses a custom domain that cannot expose ATS
    # identity from hostname alone.
    ats_type = detected if detected != "custom" else hinted
    return PostingIdentity(
        canonical_url=canonical_url,
        jd_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ats_type=ats_type,
        company_key=_norm_label(company),
        title_key=_norm_label(job_title),
    )


def find_existing_application(cur: Any, identity: PostingIdentity, *,
                              ats_company_id: str | None = None,
                              source_job_id: str | None = None) -> tuple[str, str, str, str, str] | None:
    """Return a stable existing posting without deduping unrelated boilerplate.

    Result: ``(id, company, job_title, jd_hash, current_step)``.
    """
    if ats_company_id and source_job_id:
        cur.execute(
            """SELECT id::text,coalesce(company,''),coalesce(job_title,''),coalesce(jd_hash,''),coalesce(current_step,'')
                 FROM applications
                WHERE ats_company_id=%s AND source_job_id=%s
                ORDER BY created_at DESC LIMIT 1;""",
            (ats_company_id, source_job_id),
        )
        row = cur.fetchone()
        if row:
            return tuple(str(v or "") for v in row)  # type: ignore[return-value]

    if identity.canonical_url:
        cur.execute(
            """SELECT id::text,coalesce(company,''),coalesce(job_title,''),coalesce(jd_hash,''),coalesce(current_step,'')
                 FROM applications
                WHERE coalesce(job_url,'')=%s
                ORDER BY created_at DESC LIMIT 1;""",
            (identity.canonical_url,),
        )
        row = cur.fetchone()
        if row:
            return tuple(str(v or "") for v in row)  # type: ignore[return-value]

    # No exact URL: JD hash is only safe as a duplicate key when employer/title
    # also match. This prevents two employers using the same boilerplate from
    # collapsing into one application.
    cur.execute(
        """SELECT id::text,coalesce(company,''),coalesce(job_title,''),coalesce(jd_hash,''),coalesce(current_step,'')
             FROM applications
            WHERE jd_hash=%s
              AND lower(regexp_replace(coalesce(company,''),'\\s+',' ','g'))=%s
              AND lower(regexp_replace(coalesce(job_title,''),'\\s+',' ','g'))=%s
            ORDER BY created_at DESC LIMIT 1;""",
        (identity.jd_hash, identity.company_key, identity.title_key),
    )
    row = cur.fetchone()
    return tuple(str(v or "") for v in row) if row else None  # type: ignore[return-value]
