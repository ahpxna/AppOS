-- 086 -- Reassert explicit pipeline transition-kind classification.
--
-- Runtime state transitions require pipeline_transitions.transition_kind. Some
-- installations assembled from intermediate V1 candidates can record 084 while
-- still lacking the column. Reassert the invariant idempotently instead of
-- weakening the runtime back to the legacy automated boolean.
BEGIN;

ALTER TABLE pipeline_transitions
  ADD COLUMN IF NOT EXISTS transition_kind text;

UPDATE pipeline_transitions
SET transition_kind = CASE WHEN automated THEN 'automated' ELSE 'human' END
WHERE transition_kind IS NULL;

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

ALTER TABLE pipeline_transitions ALTER COLUMN transition_kind SET NOT NULL;
ALTER TABLE pipeline_transitions DROP CONSTRAINT IF EXISTS pipeline_transitions_transition_kind_check;
ALTER TABLE pipeline_transitions ADD CONSTRAINT pipeline_transitions_transition_kind_check
  CHECK (transition_kind IN ('automated','human','privileged','recovery'));

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('pipeline_transition_kind_schema_invariant','safety','L0',
   'Guarantees every pipeline edge is explicitly classified as automated, human, privileged, or recovery.',
   false,'active','Migration 086 repairs intermediate V1 schema drift without weakening runtime checks.',now(),now())
ON CONFLICT (name) DO UPDATE SET
  purpose=EXCLUDED.purpose, status=EXCLUDED.status, notes=EXCLUDED.notes, updated_at=now();

COMMIT;
