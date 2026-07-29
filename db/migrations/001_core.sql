-- Core schema for Job Apply OS
-- Phase 1: applications, browser task queue, approvals, logs

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =========================
-- APPLICATIONS
-- =========================
CREATE TABLE IF NOT EXISTS applications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  source text,
  company text,
  job_title text,
  job_url text,
  jd_text text,
  jd_hash text,

  current_step text,
  status text,

  fit_score int,
  fit_decision text,
  priority text,

  ats_type text,
  deadline date,
  salary_range text,
  work_mode text,
  location text,
  seniority_level text,

  approved_resume_id uuid,
  approved_cover_letter_id uuid,
  approved_short_answers_id uuid,

  submitted_at timestamptz,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  last_error text
);

CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_current_step ON applications(current_step);
CREATE INDEX IF NOT EXISTS idx_applications_jd_hash ON applications(jd_hash);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);

-- =========================
-- BROWSER TASKS
-- =========================
CREATE TABLE IF NOT EXISTS browser_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_task_id uuid,

  task_type text NOT NULL,
  requested_by text NOT NULL,

  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  message_thread_id uuid,

  status text NOT NULL DEFAULT 'queued',
  priority text NOT NULL DEFAULT 'normal',

  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  result_json jsonb,
  error_message text,

  locked_by text,
  lease_expires_at timestamptz,

  retry_count int DEFAULT 0,
  max_retries int DEFAULT 2,
  timeout_seconds int DEFAULT 120,

  screenshot_url text,
  confirmation_url text,

  created_at timestamptz DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_browser_tasks_status ON browser_tasks(status);
CREATE INDEX IF NOT EXISTS idx_browser_tasks_priority_created ON browser_tasks(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_browser_tasks_lease ON browser_tasks(lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_browser_tasks_application_id ON browser_tasks(application_id);

-- =========================
-- DEAD LETTER TASKS
-- =========================
CREATE TABLE IF NOT EXISTS dead_letter_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  original_task_id uuid,
  task_type text,
  application_id uuid,
  message_thread_id uuid,

  input_json jsonb,
  last_error text,
  retry_count int,
  screenshot_url text,

  created_at timestamptz DEFAULT now()
);

-- =========================
-- APPROVAL REQUESTS
-- =========================
CREATE TABLE IF NOT EXISTS approval_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  type text NOT NULL,
  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  message_thread_id uuid,

  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  status text NOT NULL DEFAULT 'pending',
  approval_channel text NOT NULL DEFAULT 'telegram',
  approval_token_hash text NOT NULL,
  token_expires_at timestamptz NOT NULL,

  action_taken text,
  action_note text,

  notified_at timestamptz,
  reminded_at timestamptz,
  responded_at timestamptz,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status);
CREATE INDEX IF NOT EXISTS idx_approval_requests_token_hash ON approval_requests(approval_token_hash);
CREATE INDEX IF NOT EXISTS idx_approval_requests_application_id ON approval_requests(application_id);

-- =========================
-- COST LEDGER
-- =========================
CREATE TABLE IF NOT EXISTS cost_ledger (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  message_thread_id uuid,

  agent_name text,
  model_name text,

  input_tokens int,
  output_tokens int,
  estimated_cost_usd numeric,

  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_budgets (
  date date PRIMARY KEY,

  max_cost_usd numeric,
  current_cost_usd numeric DEFAULT 0,

  max_jobs_full_pipeline int,
  max_browser_tasks int
);

-- =========================
-- UPDATED_AT TRIGGER
-- =========================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_applications_updated_at ON applications;
CREATE TRIGGER trg_applications_updated_at
BEFORE UPDATE ON applications
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

