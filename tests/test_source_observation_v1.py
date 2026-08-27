from __future__ import annotations

import hashlib

from services.intake.source_observation import observe_existing_posting


class FakeCursor:
    def __init__(self, *, current_step: str, existing_hash: str, prior_revision: bool = False):
        self.current_step = current_step
        self.existing_hash = existing_hash
        self.prior_revision = prior_revision
        self._next = None
        self.statements: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.statements.append((normalized, params))
        if "FROM applications WHERE id=%s FOR UPDATE" in normalized:
            self._next = (self.current_step, self.existing_hash)
        elif "SELECT id::text FROM job_posting_source_revisions" in normalized:
            self._next = ("rev-existing",) if self.prior_revision else None
        elif normalized.startswith("INSERT INTO job_posting_source_revisions"):
            self._next = ("rev-1",)
        else:
            self._next = None

    def fetchone(self):
        value = self._next
        self._next = None
        return value


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_intake_source_refresh_promotes_in_place():
    old = _digest("old")
    new_text = "new full JD " * 30
    cur = FakeCursor(current_step="intake", existing_hash=old)
    result = observe_existing_posting(
        cur, application_id="app-1", source_name="workday", jd_text=new_text,
        jd_hash=_digest(new_text), job_url="https://example.com/job/1", company="Acme",
        job_title="Engineer", location="NY", work_mode="Hybrid",
    )
    assert result.disposition == "promoted"
    sql = "\n".join(stmt for stmt, _ in cur.statements)
    assert "SET company=coalesce(nullif(%s,''),company)" in sql and "jd_text=%s, jd_hash=%s" in sql
    assert "job_source_revision_detected" not in sql


def test_downstream_source_change_is_append_only_and_audited():
    old = _digest("old")
    new_text = "updated full JD " * 30
    cur = FakeCursor(current_step="docs_verified", existing_hash=old)
    result = observe_existing_posting(
        cur, application_id="app-1", source_name="workday", jd_text=new_text,
        jd_hash=_digest(new_text), job_url="https://example.com/job/1", company="Acme",
        job_title="Engineer", location="NY", work_mode="Hybrid",
    )
    assert result.disposition == "changed_downstream"
    sql = "\n".join(stmt for stmt, _ in cur.statements)
    assert "jd_text=%s, jd_hash=%s" not in sql
    assert "job_source_revision_detected" in sql


def test_repeated_downstream_revision_is_idempotent():
    old = _digest("old")
    new_text = "updated full JD " * 30
    cur = FakeCursor(current_step="awaiting_approval", existing_hash=old, prior_revision=True)
    result = observe_existing_posting(
        cur, application_id="app-1", source_name="linkedin", jd_text=new_text,
        jd_hash=_digest(new_text), job_url="https://www.linkedin.com/jobs/view/123",
        company="Acme", job_title="Engineer",
    )
    assert result.disposition == "changed_downstream"
    sql = "\n".join(stmt for stmt, _ in cur.statements)
    assert "job_source_revision_detected" not in sql


def test_same_downstream_hash_only_refreshes_visibility_metadata():
    text = "stable full JD " * 30
    digest = _digest(text)
    cur = FakeCursor(current_step="form_filled", existing_hash=digest)
    result = observe_existing_posting(
        cur, application_id="app-1", source_name="greenhouse", jd_text=text,
        jd_hash=digest, job_url="https://boards.greenhouse.io/acme/jobs/1",
        company="Acme", job_title="Engineer",
    )
    assert result.disposition == "unchanged"
    sql = "\n".join(stmt for stmt, _ in cur.statements)
    assert "last_seen_at=now(), stale_at=NULL, closed_at=NULL" in sql
    assert "job_source_revision_detected" not in sql
