"""Database intake for a JD pasted by the user, with no browser dependency.

This module is intentionally small and shared by the desktop form.  It writes
to the existing ``applications`` table, so existing deduplication, market
requirement extraction, fit analysis, document generation, and approval gates
continue to apply.  It never opens a URL, starts a browser, or invokes an LLM.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
import re
from typing import Any
from urllib.parse import urlparse

from psycopg.types.json import Jsonb
from services.discovery.immigration_intelligence import record_jd_immigration_assessment
from services.intake.posting_identity import build_posting_identity, find_existing_application


MIN_JD_CHARS = 200
SOURCE_OPTIONS = {"manual_paste", "linkedin_copy", "company_career_page", "recruiter", "job_board", "referral"}
WORK_MODES = {"", "remote", "hybrid", "on_site", "unknown"}


class ManualIntakeError(ValueError):
    """A form value cannot safely become an application record."""


@dataclass(frozen=True)
class JobDraft:
    company: str
    job_title: str
    jd_text: str
    job_url: str = ""
    source: str = "manual_paste"
    location: str = ""
    work_mode: str = "unknown"
    seniority_level: str = ""
    deadline: str = ""
    salary_range: str = ""
    notes: str = ""


def _clean(value: str, *, limit: int) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())[:limit]


def normalize_draft(draft: JobDraft) -> JobDraft:
    """Validate user input before any database write or paid pipeline stage."""
    company = _clean(draft.company, limit=300)
    job_title = _clean(draft.job_title, limit=300)
    jd_text = (draft.jd_text or "").strip()
    source = _clean(draft.source, limit=80).lower().replace(" ", "_") or "manual_paste"
    work_mode = _clean(draft.work_mode, limit=40).lower().replace("-", "_") or "unknown"
    job_url = (draft.job_url or "").strip()
    if not company:
        raise ManualIntakeError("Company name is required.")
    if not job_title:
        raise ManualIntakeError("Job title is required.")
    if len(jd_text) < MIN_JD_CHARS:
        raise ManualIntakeError(f"Paste at least {MIN_JD_CHARS} characters of the job description.")
    if source not in SOURCE_OPTIONS:
        raise ManualIntakeError("Choose a source from the form options.")
    if work_mode not in WORK_MODES:
        raise ManualIntakeError("Choose a work mode from the form options.")
    if job_url:
        parsed = urlparse(job_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ManualIntakeError("Job URL must begin with http:// or https://, or be left empty.")
    deadline = _clean(draft.deadline, limit=10)
    if deadline:
        try:
            date.fromisoformat(deadline)
        except ValueError as exc:
            raise ManualIntakeError("Deadline must use YYYY-MM-DD, or be left empty.") from exc
    return JobDraft(
        company=company, job_title=job_title, jd_text=jd_text, job_url=job_url,
        source=source, location=_clean(draft.location, limit=300), work_mode=work_mode,
        seniority_level=_clean(draft.seniority_level, limit=120), deadline=deadline,
        salary_range=_clean(draft.salary_range, limit=200), notes=(draft.notes or "").strip()[:4000],
    )


def create_application(cur: Any, draft: JobDraft) -> tuple[str | None, JobDraft]:
    """Create a normal application and auditable event, or return a JD duplicate.

    Migration 048's database trigger queues market-demand extraction from this
    insert.  It runs independently of the later no-LLM filter and fit result.
    """
    clean = normalize_draft(draft)
    identity = build_posting_identity(
        company=clean.company, job_title=clean.job_title, jd_text=clean.jd_text, job_url=clean.job_url,
    )
    existing = find_existing_application(cur, identity)
    if existing:
        return None, clean
    jd_hash = identity.jd_hash
    cur.execute(
        """
        INSERT INTO applications
          (source, company, job_title, job_url, jd_text, jd_hash, current_step, status,
           intake_channel, ats_type, location, work_mode, seniority_level, deadline, salary_range,
           created_at, updated_at)
        VALUES (%s, %s, %s, NULLIF(%s, ''), %s, %s, 'intake', 'active',
                'desktop_manual_form', %s, NULLIF(%s, ''), %s, NULLIF(%s, ''),
                NULLIF(%s, '')::date, NULLIF(%s, ''), now(), now())
        RETURNING id::text;
        """,
        (clean.source, clean.company, clean.job_title, identity.canonical_url, clean.jd_text, jd_hash,
         identity.ats_type, clean.location, clean.work_mode, clean.seniority_level, clean.deadline, clean.salary_range),
    )
    application_id = cur.fetchone()[0]
    immigration = record_jd_immigration_assessment(cur, application_id, clean.jd_text)
    event_detail = {
        "channel": "desktop_manual_form", "source": clean.source, "jd_hash": jd_hash,
        "ats_type": identity.ats_type, "canonical_job_url": identity.canonical_url,
        "notes": clean.notes, "browser_used": False,
        "immigration_assessment": immigration,
    }
    cur.execute(
        """INSERT INTO pipeline_events
              (application_id, from_step, to_step, actor, reason, detail_json)
           VALUES (%s, NULL, 'intake', 'manual_job_intake',
                   'User pasted and reviewed this job description.', %s);""",
        (application_id, Jsonb(event_detail)),
    )
    return application_id, clean


def public_draft_summary(draft: JobDraft) -> dict[str, str | int]:
    """Return a safe UI/report summary without returning the full pasted JD."""
    clean = normalize_draft(draft)
    summary = asdict(clean)
    summary.pop("jd_text")
    summary.pop("notes")
    summary["jd_characters"] = len(clean.jd_text)
    return summary
