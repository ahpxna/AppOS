-- =========================================================
-- 056 -- Discovery lifecycle, safe preferences, and ATS capabilities.
-- =========================================================

BEGIN;

ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS source_job_id text,
  ADD COLUMN IF NOT EXISTS first_seen_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS last_content_change_at timestamptz,
  ADD COLUMN IF NOT EXISTS closed_at timestamptz,
  ADD COLUMN IF NOT EXISTS stale_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_source_job
  ON applications (source, ats_company_id, source_job_id)
  WHERE source_job_id IS NOT NULL AND ats_company_id IS NOT NULL;

-- JD text is evidence, not a stable posting identity: two employers may post
-- identical boilerplate, and one posting may change wording over time.
DROP INDEX IF EXISTS uq_applications_jd_hash;

CREATE INDEX IF NOT EXISTS idx_applications_seen_lifecycle
  ON applications (source, last_seen_at DESC, stale_at)
  WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS job_search_preferences (
  profile_key text PRIMARY KEY DEFAULT 'primary',
  company_blacklist text[] NOT NULL DEFAULT '{}',
  title_blacklist text[] NOT NULL DEFAULT '{}',
  location_blacklist text[] NOT NULL DEFAULT '{}',
  location_allow_patterns text[] NOT NULL DEFAULT '{}',
  allowed_work_modes text[] NOT NULL DEFAULT '{remote,hybrid,on-site}',
  allowed_employment_types text[] NOT NULL DEFAULT '{full-time,contract,internship}',
  freshness_days int NOT NULL DEFAULT 30 CHECK (freshness_days BETWEEN 1 AND 365),
  salary_floor numeric,
  max_active_applications_per_employer int NOT NULL DEFAULT 2
    CHECK (max_active_applications_per_employer BETWEEN 1 AND 20),
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO job_search_preferences (profile_key) VALUES ('primary')
ON CONFLICT (profile_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS application_question_memory (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope text NOT NULL CHECK (scope IN ('global', 'ats', 'company')),
  ats_type text,
  company_normalized text,
  question_normalized text NOT NULL,
  answer_text text NOT NULL,
  answer_kind text NOT NULL DEFAULT 'text',
  confidence numeric NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  user_confirmed_at timestamptz NOT NULL,
  last_used_at timestamptz,
  use_count int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (scope, ats_type, company_normalized, question_normalized)
);

CREATE TABLE IF NOT EXISTS application_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  browser_task_id uuid REFERENCES browser_tasks(id) ON DELETE SET NULL,
  attempt_kind text NOT NULL,
  status text NOT NULL CHECK (status IN ('started', 'completed', 'partial', 'failed', 'needs_review', 'reconciled')),
  detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_application_attempts_application
  ON application_attempts(application_id, started_at DESC);

CREATE TABLE IF NOT EXISTS ats_capabilities (
  ats_type text PRIMARY KEY,
  supports_discovery boolean NOT NULL DEFAULT false,
  supports_static_text boolean NOT NULL DEFAULT false,
  supports_radio boolean NOT NULL DEFAULT false,
  supports_select boolean NOT NULL DEFAULT false,
  supports_upload boolean NOT NULL DEFAULT false,
  supports_multi_page boolean NOT NULL DEFAULT false,
  autofill_mode text NOT NULL DEFAULT 'review_only'
    CHECK (autofill_mode IN ('review_only', 'single_page', 'multi_page')),
  notes text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ats_capabilities
  (ats_type, supports_discovery, supports_static_text, supports_radio, supports_select, supports_upload, supports_multi_page, autofill_mode, notes)
VALUES
  ('greenhouse', true, true, true, true, true, false, 'single_page', 'Public discovery adapter; form support is verified per site.'),
  ('lever', true, true, true, true, true, false, 'single_page', 'Public discovery adapter; form support is verified per site.'),
  ('ashby', true, true, true, true, true, false, 'single_page', 'Public discovery adapter; form support is verified per site.'),
  ('workday', false, false, false, false, false, false, 'review_only', 'No generic browser automation support.'),
  ('linkedin_browser_linked_session', true, false, false, false, false, false, 'review_only', 'Discovery only; never application automation.')
ON CONFLICT (ats_type) DO NOTHING;

COMMIT;
