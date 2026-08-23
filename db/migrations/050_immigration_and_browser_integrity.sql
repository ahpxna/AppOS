-- 050 -- fail-closed immigration semantics and browser approval integrity
--
-- This migration repairs historical unsafe defaults from 038.  It intentionally
-- invalidates the old universal authorization/sponsorship answers: they may
-- have been seeded or overwritten by a migration rather than explicitly
-- confirmed by the candidate for the wording on a real employer form.

BEGIN;

-- A single boolean cannot faithfully represent F-1/OPT/STEM questions.  This
-- is a candidate-maintained profile, not legal advice and not an answer bot.
CREATE TABLE IF NOT EXISTS immigration_profiles (
  id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_key                     text NOT NULL UNIQUE DEFAULT 'primary',
  current_status                  text NOT NULL DEFAULT 'unconfirmed',
  current_work_authorization      text NOT NULL DEFAULT 'unconfirmed',
  opt_eligible                    boolean,
  opt_start_date                  date,
  opt_end_date                    date,
  stem_extension_eligible         boolean,
  stem_cip_code                   text,
  requires_sponsorship_to_start   text NOT NULL DEFAULT 'unconfirmed',
  requires_future_sponsorship     text NOT NULL DEFAULT 'unconfirmed',
  user_confirmed_at               timestamptz,
  confirmation_note               text,
  created_at                      timestamptz NOT NULL DEFAULT now(),
  updated_at                      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chk_immigration_current_work_authorization
    CHECK (current_work_authorization IN ('yes', 'no', 'unconfirmed')),
  CONSTRAINT chk_immigration_sponsorship_to_start
    CHECK (requires_sponsorship_to_start IN ('yes', 'no', 'unconfirmed')),
  CONSTRAINT chk_immigration_future_sponsorship
    CHECK (requires_future_sponsorship IN ('yes', 'no', 'unconfirmed'))
);

DROP TRIGGER IF EXISTS trg_immigration_profiles_updated_at ON immigration_profiles;
CREATE TRIGGER trg_immigration_profiles_updated_at
BEFORE UPDATE ON immigration_profiles
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS immigration_question_rules (
  question_class text PRIMARY KEY,
  description text NOT NULL,
  requires_user_confirmation boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO immigration_question_rules (question_class, description)
VALUES
  ('CURRENT_AUTHORIZATION', 'Current authorization to work in the United States.'),
  ('SPONSORSHIP_TO_START', 'Whether sponsorship is required to begin employment.'),
  ('SPONSORSHIP_NOW_OR_FUTURE', 'Whether sponsorship is required now or in the future.'),
  ('US_CITIZENSHIP', 'Citizenship status or citizenship requirement.'),
  ('PERMANENT_WORK_AUTHORIZATION', 'Indefinite/permanent authorization requirement.'),
  ('STEM_OPT_EMPLOYER_REQUIREMENT', 'STEM OPT/E-Verify/I-983 employer requirement.'),
  ('UNKNOWN_IMMIGRATION_QUESTION', 'Potentially immigration-related question whose semantics were not classified.')
ON CONFLICT (question_class) DO UPDATE
SET description = EXCLUDED.description,
    requires_user_confirmation = EXCLUDED.requires_user_confirmation;

-- Per-job evidence keeps explicit job-policy text separate from employer
-- evidence.  An E-Verify result is never a proxy for H-1B sponsorship.
CREATE TABLE IF NOT EXISTS application_immigration_assessments (
  application_id uuid PRIMARY KEY REFERENCES applications(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'UNKNOWN',
  jd_policy_result text NOT NULL DEFAULT 'unknown',
  jd_policy_evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  everify_status text NOT NULL DEFAULT 'unknown',
  everify_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  h1b_history_status text NOT NULL DEFAULT 'unknown',
  h1b_history_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  final_reason text,
  reviewed_by_user_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chk_immigration_assessment_status
    CHECK (status IN ('HIGH', 'POSSIBLE', 'LOW', 'BLOCKED', 'UNKNOWN')),
  CONSTRAINT chk_immigration_jd_policy
    CHECK (jd_policy_result IN ('compatible', 'incompatible', 'unknown')),
  CONSTRAINT chk_immigration_everify
    CHECK (everify_status IN ('verified', 'not_found', 'unknown')),
  CONSTRAINT chk_immigration_h1b_history
    CHECK (h1b_history_status IN ('positive', 'none_found', 'unknown'))
);

DROP TRIGGER IF EXISTS trg_application_immigration_assessments_updated_at ON application_immigration_assessments;
CREATE TRIGGER trg_application_immigration_assessments_updated_at
BEFORE UPDATE ON application_immigration_assessments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Clear migration-created generic legal answers.  A user must now confirm a
-- semantic profile and answer the exact wording in the form.  This is
-- intentionally a safe reset, not an inference about the candidate's status.
UPDATE sensitive_answers
SET answer = 'ASK_USER',
    answer_kind = 'eligibility',
    requires_review = true,
    approved_by_user = false,
    notes = 'Reset by migration 050: legal/immigration answer requires semantic user confirmation.',
    updated_at = now()
WHERE field_name IN ('work_authorization', 'visa_sponsorship', 'require_sponsorship');

-- These are review flags, never automatic rejection rules.  Whether a phrase
-- is disqualifying depends on the candidate-confirmed immigration profile and
-- the exact employer wording; the normal intake classifier preserves the
-- quote in application_immigration_assessments.
INSERT INTO no_llm_filter_rules (rule_name, rule_type, pattern, action, reason)
VALUES
  ('immigration_no_sponsorship_signal', 'jd_regex',
   '(?i)(unable to|cannot|will not|does not)\s+(provide|offer|sponsor).{0,80}(visa|sponsorship)|must not.{0,80}(require|need).{0,80}(sponsorship|visa)',
   'flag', 'JD contains an explicit sponsorship restriction; review against the candidate-confirmed immigration profile.'),
  ('immigration_permanent_authorization_signal', 'jd_regex',
   '(?i)(permanent|indefinite)\s+(work\s+)?authorization|authorized\s+to\s+work.{0,80}(indefinitely|without sponsorship)',
   'flag', 'JD may require permanent/indefinite work authorization; verify exact wording manually.'),
  ('immigration_citizenship_signal', 'jd_regex',
   '(?i)(u\.?s\.? citizen(ship)? required|u\.?s\.? person required|citizenship required)',
   'flag', 'JD contains a citizenship/US-person restriction; verify eligibility manually.')
ON CONFLICT (rule_name) DO UPDATE
SET pattern = EXCLUDED.pattern, action = EXCLUDED.action, reason = EXCLUDED.reason;

-- Bind a human approval to one exact browser capability.  Existing approval
-- rows remain readable but cannot authorize form writes until reissued.
ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS bound_document_id uuid REFERENCES generated_documents(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS bound_document_sha256 text,
  ADD COLUMN IF NOT EXISTS expected_origin text;

-- 037 incorrectly treated token redemption as capability consumption.  A
-- redeemed approval is ready to be consumed by one exact side effect; it is
-- not consumed until the worker atomically claims that side effect.
ALTER TABLE approval_requests
  DROP CONSTRAINT IF EXISTS chk_approval_single_use;
ALTER TABLE approval_requests
  ADD CONSTRAINT chk_approval_single_use
  CHECK (
    status IN ('pending', 'approved', 'denied', 'expired')
    OR (status = 'consumed' AND consumed_at IS NOT NULL)
  );

ALTER TABLE browser_tasks
  ADD COLUMN IF NOT EXISTS expected_origin text,
  ADD COLUMN IF NOT EXISTS generated_document_id uuid REFERENCES generated_documents(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS document_sha256 text;

CREATE INDEX IF NOT EXISTS idx_browser_tasks_approval_binding
  ON browser_tasks(approval_request_id, application_id)
  WHERE approval_request_id IS NOT NULL;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('immigration_semantics_gate', 'safety', 'L7',
   'Classify immigration question semantics and pause unless the candidate confirms the exact answer.',
   false, 'active',
   'Never maps an E-Verify result to sponsorship and never answers a legal question from keyword matching alone.', now(), now()),
  ('browser_capability_binding', 'safety', 'L3',
   'Bind approval, application, document hash and expected origin to one browser capability.',
   false, 'active',
   'Legacy approvals without a complete binding cannot authorize form writes.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
