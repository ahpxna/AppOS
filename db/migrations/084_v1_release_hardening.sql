-- 084 -- V1 release hardening: ATS capability semantics, document-attempt
-- recovery leases, and explicit transition kinds.  This migration corrects
-- semantics introduced by 083 without rewriting already-applied history.
BEGIN;

-- ATS identity/discovery support is distinct from vendor-specific browser
-- certification.  generic_accessibility means JobOS may use only the shared
-- deterministic accessibility primitive engine and must safe-pause on controls
-- it cannot classify; it does NOT claim vendor-specific selectors are proven.
ALTER TABLE ats_capabilities
  ADD COLUMN IF NOT EXISTS verification_level text NOT NULL DEFAULT 'review_only';

ALTER TABLE ats_capabilities DROP CONSTRAINT IF EXISTS ats_capabilities_verification_level_check;
ALTER TABLE ats_capabilities ADD CONSTRAINT ats_capabilities_verification_level_check
  CHECK (verification_level IN ('review_only','generic_accessibility','fixture_verified','live_verified'));

UPDATE ats_capabilities
SET verification_level = CASE
      WHEN autofill_mode='review_only' THEN 'review_only'
      ELSE 'generic_accessibility'
    END,
    notes = regexp_replace(
      coalesce(notes,''),
      '^Registry-driven generic browser support; ',
      'Registry identity; generic accessibility browser support; '
    ),
    updated_at = now();

INSERT INTO ats_capabilities
  (ats_type, supports_discovery, supports_static_text, supports_radio, supports_select,
   supports_upload, supports_multi_page, autofill_mode, verification_level, notes)
VALUES
  ('darwinbox', true, true, true, true, true, true, 'generic_browser', 'generic_accessibility',
   'Structured-web discovery plus generic accessibility browser support; no vendor-specific selector certification.'),
  ('spark_hire', true, true, true, true, true, true, 'generic_browser', 'generic_accessibility',
   'Structured-web discovery plus generic accessibility browser support; no vendor-specific selector certification.'),
  ('eploy', true, true, true, true, true, true, 'generic_browser', 'generic_accessibility',
   'Structured-web discovery plus generic accessibility browser support; no vendor-specific selector certification.')
ON CONFLICT (ats_type) DO UPDATE SET
  supports_discovery=EXCLUDED.supports_discovery,
  supports_static_text=EXCLUDED.supports_static_text,
  supports_radio=EXCLUDED.supports_radio,
  supports_select=EXCLUDED.supports_select,
  supports_upload=EXCLUDED.supports_upload,
  supports_multi_page=EXCLUDED.supports_multi_page,
  autofill_mode=EXCLUDED.autofill_mode,
  verification_level=EXCLUDED.verification_level,
  notes=EXCLUDED.notes,
  updated_at=now();

INSERT INTO allowed_domains(domain,category,notes) VALUES
  ('darwinbox.in','ats','ATS registry: Darwinbox Recruiting'),
  ('sparkhire.com','ats','ATS registry: Spark Hire Recruit'),
  ('eploy.co.uk','ats','ATS registry: Eploy')
ON CONFLICT (domain) DO UPDATE SET
  category=EXCLUDED.category, notes=EXCLUDED.notes, enabled=true;

-- Source boards remain mutable after an application has entered the evidence
-- pipeline. Keep later observations append-only so a poll can never rewrite the
-- JD/hash already bound to fit analysis, documents, approval, or browser work.
CREATE TABLE IF NOT EXISTS job_posting_source_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  source_name text NOT NULL,
  source_job_id text,
  canonical_url text NOT NULL DEFAULT '',
  company text NOT NULL DEFAULT '',
  job_title text NOT NULL DEFAULT '',
  location text NOT NULL DEFAULT '',
  work_mode text NOT NULL DEFAULT 'unknown',
  jd_hash char(64) NOT NULL,
  jd_text text NOT NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  observed_at timestamptz NOT NULL DEFAULT now(),
  promoted_at timestamptz,
  UNIQUE(application_id, source_name, jd_hash)
);

CREATE INDEX IF NOT EXISTS idx_job_posting_source_revisions_application
  ON job_posting_source_revisions(application_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_posting_source_revisions_pending
  ON job_posting_source_revisions(application_id, observed_at DESC)
  WHERE promoted_at IS NULL;

-- A model response has no durable external artifact until generated_documents
-- is committed.  A crashed/uncertain attempt therefore needs a bounded lease,
-- not an eternal lock. Paid-call uncertainty is still accounted separately in
-- llm_cost_reservations, so replay remains budget-governed.
ALTER TABLE document_generation_attempts
  ADD COLUMN IF NOT EXISTS attempt_count int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

UPDATE document_generation_attempts
SET attempt_count = GREATEST(attempt_count, 1),
    lease_expires_at = CASE
      WHEN status='running' THEN coalesce(lease_expires_at, updated_at + interval '45 minutes')
      ELSE NULL
    END;

CREATE INDEX IF NOT EXISTS idx_document_generation_attempts_lease
  ON document_generation_attempts(status, lease_expires_at)
  WHERE status='running';

-- Preserve the old automated boolean for compatibility, but make recovery a
-- first-class contract so workers no longer masquerade as human actors.
ALTER TABLE pipeline_transitions
  ADD COLUMN IF NOT EXISTS transition_kind text;

UPDATE pipeline_transitions
SET transition_kind = CASE WHEN automated THEN 'automated' ELSE 'human' END
WHERE transition_kind IS NULL;

ALTER TABLE pipeline_transitions ALTER COLUMN transition_kind SET NOT NULL;
ALTER TABLE pipeline_transitions DROP CONSTRAINT IF EXISTS pipeline_transitions_transition_kind_check;
ALTER TABLE pipeline_transitions ADD CONSTRAINT pipeline_transitions_transition_kind_check
  CHECK (transition_kind IN ('automated','human','privileged','recovery'));

-- Browser/auth progression is executed only inside a separately approved
-- one-shot privileged capability.  It is neither an orchestrator edge nor the
-- human decision itself, so model it explicitly rather than overloading the
-- legacy automated boolean.
UPDATE pipeline_transitions
SET transition_kind='privileged'
WHERE (from_step,to_step) IN (
  ('docs_verified','application_entrypoint_ready'),
  ('application_entrypoint_ready','needs_account_auth'),
  ('application_entrypoint_ready','needs_email_verification'),
  ('application_entrypoint_ready','needs_mfa'),
  ('application_entrypoint_ready','needs_human_checkpoint'),
  ('application_entrypoint_ready','application_form_ready'),
  ('needs_account_auth','needs_email_verification'),
  ('needs_account_auth','needs_mfa'),
  ('needs_account_auth','needs_human_checkpoint'),
  ('needs_account_auth','application_form_ready'),
  ('needs_email_verification','needs_mfa'),
  ('needs_email_verification','needs_human_checkpoint'),
  ('needs_email_verification','application_form_ready'),
  ('needs_mfa','needs_human_checkpoint'),
  ('needs_mfa','application_form_ready'),
  ('needs_human_checkpoint','application_form_ready'),
  ('application_ready','application_form_ready'),
  ('application_ready','submitted')
);

-- Packaging an exact form into a pending approval is an automated
-- materialization step.  The subsequent approval decision remains human.
UPDATE pipeline_transitions
SET transition_kind='automated'
WHERE from_step='application_form_ready' AND to_step='awaiting_approval';

UPDATE pipeline_transitions
SET transition_kind='recovery'
WHERE (from_step,to_step) IN (
  ('autofill_executing','awaiting_approval'),
  ('autofill_executing','application_form_ready'),
  ('needs_email_verification','needs_account_auth'),
  ('needs_mfa','needs_account_auth'),
  ('needs_human_checkpoint','needs_mfa'),
  ('needs_human_checkpoint','needs_email_verification'),
  ('needs_mfa','needs_email_verification'),
  ('needs_email_verification','needs_human_checkpoint'),
  ('awaiting_approval','application_form_ready')
);

COMMIT;
