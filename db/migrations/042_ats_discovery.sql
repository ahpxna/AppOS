-- =========================================================
-- 042_ats_discovery.sql
-- L0/L1 -- ATS API DISCOVERY
--
-- Public, unauthenticated read endpoints per ATS platform, so job
-- discovery never has to scrape LinkedIn (banned in the ToS, detected by
-- Chrome-via-CDP, and the single highest-risk thing this system could do).
--
-- ats_companies is NOT seeded with any real company slugs. Guessing
-- plausible-looking slugs and writing them into the database would be
-- worse than an empty table: a wrong slug either 404s (harmless, just
-- noisy) or silently matches a DIFFERENT company that happens to use the
-- same slug on a different platform. Add real companies yourself with:
--   python services/discovery/ats_discovery_v1.py add \
--       --company "Acme Corp" --platform greenhouse --slug acme --apply
-- Find the slug by opening the company's careers page and looking at the
-- URL: boards.greenhouse.io/<slug>, jobs.lever.co/<slug>,
-- jobs.ashbyhq.com/<slug>, <slug>.recruitee.com, <slug>.breezy.hr, etc.
-- =========================================================

BEGIN;

CREATE TABLE IF NOT EXISTS ats_companies (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_name     text NOT NULL,
  ats_platform     text NOT NULL,
    -- greenhouse / lever / ashby / smartrecruiters / recruitee / workable / breezy
  slug             text NOT NULL,
  enabled          boolean NOT NULL DEFAULT true,
  notes            text,
  last_polled_at   timestamptz,
  last_success_at  timestamptz,
  last_job_count   int,
  consecutive_failures int NOT NULL DEFAULT 0,
  created_at       timestamptz DEFAULT now(),
  updated_at       timestamptz DEFAULT now(),
  UNIQUE (ats_platform, slug)
);

CREATE INDEX IF NOT EXISTS idx_ats_companies_enabled
  ON ats_companies(enabled);

COMMENT ON TABLE ats_companies IS
  'Companies to poll for new postings via public ATS read APIs. Empty on '
  'install by design -- see migration header. consecutive_failures lets '
  'poll skip/flag a slug that has gone stale (renamed, migrated ATS, etc.) '
  'without silently retrying it forever.';

CREATE TABLE IF NOT EXISTS ats_discovery_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ats_company_id   uuid REFERENCES ats_companies(id) ON DELETE CASCADE,
  started_at       timestamptz DEFAULT now(),
  finished_at      timestamptz,
  ok               boolean,
  jobs_seen        int NOT NULL DEFAULT 0,
  jobs_new         int NOT NULL DEFAULT 0,
  jobs_duplicate   int NOT NULL DEFAULT 0,
  error            text
);

CREATE INDEX IF NOT EXISTS idx_ats_discovery_runs_company
  ON ats_discovery_runs(ats_company_id, started_at);

COMMENT ON TABLE ats_discovery_runs IS
  'One row per poll attempt per company. Kept for the same reason '
  'component_runs exists elsewhere: without it, "did discovery actually '
  'run, and did it find anything" is a question with no answer.';

-- New postings land in `applications` at current_step='intake', exactly
-- like a hand-entered JD, so the existing no_llm_filter_rules / L5 fit
-- gate / everything downstream applies unmodified. No separate `jobs`
-- table, consistent with the decision already made in 035_control_plane.sql.
ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS ats_company_id uuid REFERENCES ats_companies(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS ats_external_id text;

CREATE INDEX IF NOT EXISTS idx_applications_ats_company
  ON applications(ats_company_id);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('ats_discovery_greenhouse', 'service', 'L0', 'Poll Greenhouse Job Board API.', false, 'active', 'boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true, no auth for reads.', now(), now()),
  ('ats_discovery_lever', 'service', 'L0', 'Poll Lever Postings API.', false, 'active', 'api.lever.co/v0/postings/{slug}?mode=json, no auth for reads.', now(), now()),
  ('ats_discovery_ashby', 'service', 'L0', 'Poll Ashby Job Board API.', false, 'active', 'api.ashbyhq.com/posting-api/job-board/{slug}, no auth for reads.', now(), now()),
  ('ats_discovery_smartrecruiters', 'service', 'L0', 'Poll SmartRecruiters Posting API.', false, 'active', 'api.smartrecruiters.com/v1/companies/{slug}/postings, no auth for reads.', now(), now()),
  ('ats_discovery_recruitee', 'service', 'L0', 'Poll Recruitee public offers API.', false, 'active', '{slug}.recruitee.com/api/offers/, no auth for reads.', now(), now()),
  ('ats_discovery_workable', 'service', 'L0', 'Poll Workable public widget API.', false, 'active', 'apply.workable.com/api/v1/widget/accounts/{slug}, no auth for reads.', now(), now()),
  ('ats_discovery_breezy', 'service', 'L0', 'Poll Breezy public JSON feed.', false, 'active', '{slug}.breezy.hr/json, no auth for reads.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
