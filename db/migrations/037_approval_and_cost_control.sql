-- =========================================================
-- 037_approval_and_cost_control.sql
-- L1 -- APPROVAL SERVICE + COST CONTROLLER
--
-- Approval Service:
--   approval_requests already exists with approval_token_hash and
--   token_expires_at. This migration adds what the service needs to be
--   safe: single-use enforcement, an audit trail, and a view of what is
--   actually actionable right now.
--
--   Tokens are stored as sha256 hashes only. The plaintext token is shown
--   once at creation and never persisted, so a database read cannot be
--   replayed into an approval.
--
-- Cost Controller:
--   cost_ledger and daily_budgets exist but nothing writes to them.
--   Adds model pricing (local models cost zero, which is the point of
--   running them) and the rollup views the controller checks before
--   letting an expensive step run.
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- 1. Approval service
-- ---------------------------------------------------------

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS consumed_at     timestamptz,
  ADD COLUMN IF NOT EXISTS consumed_by     text,
  ADD COLUMN IF NOT EXISTS attempt_count   int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_attempts    int NOT NULL DEFAULT 5,
  ADD COLUMN IF NOT EXISTS requested_by    text,
  ADD COLUMN IF NOT EXISTS summary_text    text;

-- A token may be redeemed exactly once. Without this, an approval token
-- captured from a chat log could be replayed indefinitely.
ALTER TABLE approval_requests
  DROP CONSTRAINT IF EXISTS chk_approval_single_use;
ALTER TABLE approval_requests
  ADD CONSTRAINT chk_approval_single_use
  CHECK (status = 'pending' OR consumed_at IS NOT NULL OR status = 'expired');

CREATE INDEX IF NOT EXISTS idx_approval_requests_pending
  ON approval_requests(status, token_expires_at);

CREATE TABLE IF NOT EXISTS approval_events (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  approval_request_id uuid REFERENCES approval_requests(id) ON DELETE CASCADE,
  event               text NOT NULL,   -- created / approved / denied / expired / bad_token
  actor               text,
  detail_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_events_request
  ON approval_events(approval_request_id, created_at);

CREATE OR REPLACE VIEW v_approvals_actionable AS
SELECT
  ar.id AS approval_request_id,
  ar.type,
  ar.application_id,
  a.company,
  a.job_title,
  ar.summary_text,
  ar.token_expires_at,
  ar.attempt_count,
  ar.created_at
FROM approval_requests ar
LEFT JOIN applications a ON a.id = ar.application_id
WHERE ar.status = 'pending'
  AND ar.token_expires_at > now()
  AND ar.attempt_count < ar.max_attempts
ORDER BY ar.created_at;

COMMENT ON VIEW v_approvals_actionable IS
  'Pending approvals that have not expired and have attempts left.';

-- ---------------------------------------------------------
-- 2. Cost controller
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_pricing (
  model_name          text PRIMARY KEY,
  provider            text NOT NULL,
  input_usd_per_1k    numeric NOT NULL DEFAULT 0,
  output_usd_per_1k   numeric NOT NULL DEFAULT 0,
  is_local            boolean NOT NULL DEFAULT false,
  notes               text,
  updated_at          timestamptz DEFAULT now()
);

-- Local models are free at the margin. Recording them at zero is not a
-- rounding shortcut: it is the reason the pipeline defaults to Ollama for
-- the per-claim verifier, which would otherwise be the most expensive
-- component by call count.
INSERT INTO model_pricing (model_name, provider, input_usd_per_1k, output_usd_per_1k, is_local, notes)
VALUES
  ('qwen3:8b',           'ollama', 0, 0, true,  'Local. Electricity only.'),
  ('qwen3:4b',           'ollama', 0, 0, true,  'Local.'),
  ('deepseek-r1:14b',    'ollama', 0, 0, true,  'Local.'),
  ('nomic-embed-text',   'ollama', 0, 0, true,  'Local embeddings.'),
  ('openrouter/free',    'openrouter', 0, 0, false, 'Free tier. Quality varies run to run.'),
  ('unknown',            'unknown', 0, 0, false, 'Fallback when the model is unrecognised.')
ON CONFLICT (model_name) DO NOTHING;

-- Paid models: update these from the provider's pricing page before relying
-- on the numbers. They are seeded at zero rather than guessed, so an
-- unmaintained row cannot silently under-report spend.
INSERT INTO model_pricing (model_name, provider, input_usd_per_1k, output_usd_per_1k, is_local, notes)
VALUES
  ('openrouter/google/gemini-2.5-flash', 'openrouter', 0, 0, false,
   'PRICING NOT SET. Update before trusting cost reports.'),
  ('openrouter/auto', 'openrouter', 0, 0, false,
   'PRICING NOT SET. Routes to varying models; cost is approximate at best.')
ON CONFLICT (model_name) DO NOTHING;

ALTER TABLE cost_ledger
  ADD COLUMN IF NOT EXISTS component_run_id uuid,
  ADD COLUMN IF NOT EXISTS task_type        text,
  ADD COLUMN IF NOT EXISTS is_local         boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_cost_ledger_created
  ON cost_ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_application
  ON cost_ledger(application_id);

-- Prevent double-billing the same component run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_ledger_component_run
  ON cost_ledger(component_run_id) WHERE component_run_id IS NOT NULL;

ALTER TABLE daily_budgets
  ADD COLUMN IF NOT EXISTS current_jobs_full_pipeline int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS current_browser_tasks      int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS notes                      text;

-- Today's budget, created on demand by the controller.
INSERT INTO daily_budgets (date, max_cost_usd, max_jobs_full_pipeline, max_browser_tasks)
VALUES (CURRENT_DATE, 2.00, 20, 50)
ON CONFLICT (date) DO NOTHING;

CREATE OR REPLACE VIEW v_cost_today AS
SELECT
  CURRENT_DATE                                   AS date,
  COALESCE(SUM(cl.estimated_cost_usd), 0)        AS spent_usd,
  COUNT(*)                                       AS calls,
  COUNT(*) FILTER (WHERE cl.is_local)            AS local_calls,
  COUNT(*) FILTER (WHERE NOT cl.is_local)        AS paid_calls,
  COALESCE(SUM(cl.input_tokens), 0)              AS input_tokens,
  COALESCE(SUM(cl.output_tokens), 0)             AS output_tokens
FROM cost_ledger cl
WHERE cl.created_at::date = CURRENT_DATE;

CREATE OR REPLACE VIEW v_cost_by_component AS
SELECT
  cl.agent_name,
  cl.model_name,
  cl.is_local,
  COUNT(*)                                AS calls,
  COALESCE(SUM(cl.estimated_cost_usd), 0) AS total_usd,
  COALESCE(SUM(cl.input_tokens), 0)       AS input_tokens,
  COALESCE(SUM(cl.output_tokens), 0)      AS output_tokens
FROM cost_ledger cl
GROUP BY cl.agent_name, cl.model_name, cl.is_local
ORDER BY total_usd DESC, calls DESC;

CREATE OR REPLACE VIEW v_cost_by_application AS
SELECT
  a.id AS application_id,
  a.company,
  a.job_title,
  a.current_step,
  COUNT(cl.id)                            AS calls,
  COALESCE(SUM(cl.estimated_cost_usd), 0) AS total_usd
FROM applications a
LEFT JOIN cost_ledger cl ON cl.application_id = a.id
GROUP BY a.id, a.company, a.job_title, a.current_step
ORDER BY total_usd DESC;

-- ---------------------------------------------------------
-- 3. Register components
-- ---------------------------------------------------------

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('approval_service', 'safety', 'L1',
   'Issue and redeem single-use, expiring approval tokens for human gates.',
   false, 'prototype',
   'Stores sha256 hashes only. Plaintext token is shown once and never saved.',
   now(), now()),
  ('cost_controller', 'service', 'L1',
   'Record model spend and refuse work that would exceed the daily budget.',
   false, 'prototype',
   'Local models are recorded at zero cost.',
   now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
