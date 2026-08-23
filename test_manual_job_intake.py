"""No-DB tests for the user-pasted job intake boundary."""

import pytest

from services.intake.manual_job_intake import JobDraft, ManualIntakeError, normalize_draft, public_draft_summary


def valid_draft(**overrides):
    values = {
        "company": "Example Company", "job_title": "Security Engineer",
        "jd_text": "Build secure systems and document controls. " * 8,
        "source": "company_career_page", "work_mode": "hybrid",
        "job_url": "https://jobs.example.test/123", "deadline": "2026-09-01",
    }
    values.update(overrides)
    return JobDraft(**values)


def test_manual_intake_accepts_metadata_without_a_browser():
    clean = normalize_draft(valid_draft())
    summary = public_draft_summary(clean)
    assert clean.company == "Example Company"
    assert summary["jd_characters"] >= 200
    assert "jd_text" not in summary


def test_manual_intake_rejects_short_or_malformed_posting_data():
    with pytest.raises(ManualIntakeError, match="at least"):
        normalize_draft(valid_draft(jd_text="too short"))
    with pytest.raises(ManualIntakeError, match="http"):
        normalize_draft(valid_draft(job_url="jobs.example.test/123"))
    with pytest.raises(ManualIntakeError, match="YYYY-MM-DD"):
        normalize_draft(valid_draft(deadline="next week"))
