-- =========================================================
-- 035_control_plane.sql
-- L1 -- CONTROL PLANE
--
-- Adds:
--   * an explicit state machine over applications.current_step
--   * no_llm_filter_rules: cheap deterministic rejects that run
--     BEFORE any model is called
--   * pipeline_events: append-only audit of every transition
--
-- Deliberately NOT added: a separate `jobs` table. `applications`
-- already carries source/company/job_url/jd_text/jd_hash/ats_type.
-- Splitting the same entity across two tables buys nothing but joins.
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- 1. State machine vocabulary
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS pipeline_steps (
  step            text PRIMARY KEY,
  layer           text NOT NULL,
  description     text,
  is_terminal     boolean NOT NULL DEFAULT false,
  requires_human  boolean NOT NULL DEFAULT false,
  sort_order      int NOT NULL
);

INSERT INTO pipeline_steps (step, layer, description, is_terminal, requires_human, sort_order)
VALUES
  ('intake',            'L1', 'Job captured, not yet screened.',                false, false, 10),
  ('filtered_out',      'L1', 'Rejected by deterministic rules. No model spend.', true,  false, 20),
  ('screened',          'L1', 'Passed the no-LLM filter.',                      false, false, 30),
  ('fit_analyzed',      'L5', 'Fit analysis complete.',                         false, false, 40),
  ('fit_rejected',      'L5', 'Model judged the role a poor match.',            true,  false, 50),
  ('docs_generated',    'L6', 'Draft documents produced.',                      false, false, 60),
  ('docs_verified',     'L6', 'Documents passed the truth checker.',            false, false, 70),
  ('docs_failed_qa',    'L6', 'Documents could not be grounded. Needs review.', false, true,  75),
  ('awaiting_approval', 'L1', 'Waiting on the user to approve sending.',        false, true,  80),
  ('form_filled',       'L3', 'Form populated. Submit is a human action.',      false, true,  90),
  ('submitted',         'L1', 'User submitted the application.',                true,  true, 100),
  ('abandoned',         'L1', 'Dropped by the user.',                           true,  true, 110),
  ('error',             'L1', 'Halted on an unrecoverable error.',              true,  true, 120)
ON CONFLICT (step) DO UPDATE
SET layer = EXCLUDED.layer,
    description = EXCLUDED.description,
    is_terminal = EXCLUDED.is_terminal,
    requires_human = EXCLUDED.requires_human,
    sort_order = EXCLUDED.sort_order;

-- Legal transitions. The orchestrator refuses anything not listed here,
-- so a bug cannot silently skip the truth checker or the approval gate.
CREATE TABLE IF NOT EXISTS pipeline_transitions (
  from_step   text NOT NULL REFERENCES pipeline_steps(step),
  to_step     text NOT NULL REFERENCES pipeline_steps(step),
  automated   boolean NOT NULL DEFAULT true,
  note        text,
  PRIMARY KEY (from_step, to_step)
);

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('intake',            'filtered_out',      true,  'Deterministic rule matched.'),
  ('intake',            'screened',          true,  'Passed all filter rules.'),
  ('screened',          'fit_analyzed',      true,  'L5 ran.'),
  ('screened',          'fit_rejected',      true,  'L5 said reject.'),
  ('fit_analyzed',      'docs_generated',    true,  'L6 generated drafts.'),
  ('docs_generated',    'docs_verified',     true,  'Truth checker passed.'),
  ('docs_generated',    'docs_failed_qa',    true,  'Truth checker failed.'),
  ('docs_failed_qa',    'docs_generated',    false, 'User fixed assets, regenerate.'),
  ('docs_verified',     'awaiting_approval', true,  'Queued for human approval.'),
  ('awaiting_approval', 'form_filled',       false, 'Human approved; L3 filled form.'),
  ('awaiting_approval', 'abandoned',         false, 'Human declined.'),
  ('form_filled',       'submitted',         false, 'Human clicked submit.'),
  ('form_filled',       'abandoned',         false, 'Human backed out.'),
  ('intake',            'abandoned',         false, 'Manual drop.'),
  ('screened',          'abandoned',         false, 'Manual drop.'),
  ('fit_analyzed',      'abandoned',         false, 'Manual drop.'),
  ('intake',            'error',             true,  'Unrecoverable.'),
  ('screened',          'error',             true,  'Unrecoverable.'),
  ('fit_analyzed',      'error',             true,  'Unrecoverable.'),
  ('docs_generated',    'error',             true,  'Unrecoverable.')
ON CONFLICT (from_step, to_step) DO UPDATE
SET automated = EXCLUDED.automated, note = EXCLUDED.note;

-- Note the three transitions with automated=false into and out of
-- awaiting_approval / form_filled / submitted. Nothing machine-driven can
-- reach 'submitted'. That is the point.

-- ---------------------------------------------------------
-- 2. No-LLM filter
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS no_llm_filter_rules (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_name    text NOT NULL UNIQUE,
  rule_type    text NOT NULL,
    -- jd_regex / title_regex / company_blocklist / location_regex / min_jd_length
  pattern      text NOT NULL,
  action       text NOT NULL DEFAULT 'reject',   -- reject / flag
  reason       text NOT NULL,
  enabled      boolean NOT NULL DEFAULT true,
  hit_count    int NOT NULL DEFAULT 0,
  created_at   timestamptz DEFAULT now()
);

-- Seed rules. These are cheap string checks, not judgements. Anything
-- requiring interpretation belongs in L5 where a model can weigh it.
INSERT INTO no_llm_filter_rules (rule_name, rule_type, pattern, action, reason)
VALUES
  ('jd_too_short', 'min_jd_length', '200', 'reject',
   'Job description too short to analyse meaningfully.'),

  ('requires_clearance', 'jd_regex',
   '(?i)(active\s+)?(security\s+)?clearance\s+(required|mandatory)|TS/SCI|top\s+secret',
   'reject', 'Requires a government clearance the candidate does not hold.'),

  ('senior_only_title', 'title_regex',
   '(?i)\b(principal|staff|director|head\s+of|vp\s+of|chief)\b',
   'reject', 'Seniority far beyond a new-graduate profile.'),

  ('years_experience_high', 'jd_regex',
   '(?i)\b(8|9|10|11|12|15)\+?\s*years?\b',
   'reject', 'Experience requirement well beyond the profile.'),

  ('unpaid_or_commission', 'jd_regex',
   '(?i)(unpaid\s+intern|commission[- ]only|no\s+salary|equity[- ]only)',
   'reject', 'Unpaid or commission-only.'),

  ('mlm_signals', 'jd_regex',
   '(?i)(be\s+your\s+own\s+boss|unlimited\s+earning|financial\s+freedom|recruit\s+your\s+own\s+team)',
   'reject', 'Pattern consistent with MLM recruiting rather than employment.')
ON CONFLICT (rule_name) DO NOTHING;

-- ---------------------------------------------------------
-- 3. Append-only event log
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS pipeline_events (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,
  from_step      text,
  to_step        text,
  actor          text NOT NULL,          -- orchestrator / human / component name
  reason         text,
  detail_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_application
  ON pipeline_events(application_id, created_at);

-- ---------------------------------------------------------
-- 4. Intake support
-- ---------------------------------------------------------

-- jd_hash already exists on applications; make dedupe enforceable.
CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_jd_hash
  ON applications(jd_hash) WHERE jd_hash IS NOT NULL;

ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS filter_result   jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS intake_channel  text;

-- ---------------------------------------------------------
-- 5. Operator views
-- ---------------------------------------------------------

CREATE OR REPLACE VIEW v_pipeline_board AS
SELECT
  ps.sort_order,
  ps.step,
  ps.layer,
  ps.requires_human,
  count(a.id) AS application_count
FROM pipeline_steps ps
LEFT JOIN applications a ON a.current_step = ps.step
GROUP BY ps.sort_order, ps.step, ps.layer, ps.requires_human
ORDER BY ps.sort_order;

CREATE OR REPLACE VIEW v_applications_actionable AS
SELECT
  a.id AS application_id,
  a.company,
  a.job_title,
  a.current_step,
  ps.layer,
  ps.requires_human,
  a.fit_score,
  a.updated_at
FROM applications a
JOIN pipeline_steps ps ON ps.step = a.current_step
WHERE ps.is_terminal = false
ORDER BY ps.sort_order, a.updated_at;

COMMIT;
