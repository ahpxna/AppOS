-- 079 -- Final pipeline identity/liveness hardening and human document feedback loop.
BEGIN;

-- Remove legacy direct edges superseded by the durable execution/review
-- lifecycle. Keeping them in the transition graph would let generic state
-- code bypass the first-I/O autofill fence or the post-fill human review.
DELETE FROM pipeline_transitions
 WHERE (from_step='awaiting_approval' AND to_step='form_filled')
    OR (from_step='form_filled' AND to_step='submitted');

-- Refresh legacy step metadata so operator/status surfaces describe the
-- current architecture instead of the pre-auth/pre-autofill control plane.
UPDATE pipeline_steps
   SET description='Documents passed machine QA; exact human document review/open-Apply handoff is required.',
       requires_human=true
 WHERE step='docs_verified';
UPDATE pipeline_steps
   SET description='Exact current application-form plan is waiting on human approval before deterministic browser writes.',
       requires_human=true
 WHERE step='awaiting_approval';
UPDATE pipeline_steps
   SET description='Deterministic autofill finished; human post-fill review is required before any Next/Submit action.',
       requires_human=true
 WHERE step='form_filled';

-- Active autofill capabilities created before exact target-id binding cannot be
-- executed safely after this release. Expire them and recover the application
-- to a fresh planning boundary when there is no running browser task.
WITH stale_parent AS (
  UPDATE approval_requests
     SET status='expired',executing_task_id=NULL,
         action_note=coalesce(action_note,'') || ' Expired by migration 079: exact browser target binding is now required.'
   WHERE type='autofill_form' AND status IN ('pending','approved')
     AND coalesce(payload_json->>'expected_target_id','')=''
   RETURNING id,application_id
), closed_children AS (
  UPDATE approval_requests child
     SET status='expired',executing_task_id=NULL,
         action_note=coalesce(child.action_note,'') || ' Parent autofill capability was expired by migration 079.'
    FROM stale_parent parent
   WHERE child.application_id=parent.application_id
     AND child.type='privileged_upload_document'
     AND child.payload_json->>'parent_approval_request_id'=parent.id::text
     AND child.status IN ('pending','approved')
   RETURNING child.id
), dead_tasks AS (
  UPDATE browser_tasks bt
     SET status='dead_letter',finished_at=now(),
         error_message='Autofill task predates exact browser-target binding; prepare a fresh plan.'
    FROM stale_parent parent
   WHERE bt.approval_request_id=parent.id AND bt.task_type='fill_application_form'
     AND bt.status='queued'
   RETURNING bt.id
), restored AS (
  UPDATE applications a
     SET current_step='application_form_ready',updated_at=now()
   WHERE a.current_step='awaiting_approval'
     AND EXISTS (SELECT 1 FROM stale_parent sp WHERE sp.application_id=a.id)
     AND NOT EXISTS (
       SELECT 1 FROM approval_requests ar
        WHERE ar.application_id=a.id AND ar.type='autofill_form'
          AND ar.status IN ('pending','approved','executing')
          AND coalesce(ar.payload_json->>'expected_target_id','')<>''
     )
     AND NOT EXISTS (
       SELECT 1 FROM browser_tasks bt
        WHERE bt.application_id=a.id AND bt.task_type='fill_application_form'
          AND bt.status='running'
     )
   RETURNING a.id
)
INSERT INTO pipeline_events(application_id,from_step,to_step,actor,reason,detail_json)
SELECT id,'awaiting_approval','application_form_ready','migration-079',
       'Expired legacy autofill capability without exact browser-target binding; fresh plan required.',
       '{"legacy_autofill_recovered":true}'::jsonb
  FROM restored;

-- A partial/reviewed autofill may be deliberately replanned from the same
-- application page after human input. This is a fresh-approval boundary, not
-- a replay of the previous browser capability.
INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('awaiting_approval', 'application_form_ready', false, 'Human resolved paused questions or requested a fresh deterministic autofill plan.'),
  ('form_filled', 'application_form_ready', false, 'Human requested a fresh deterministic plan after reviewing the filled form.'),
  ('docs_verified', 'abandoned', false, 'Human stopped the application before opening Apply.'),
  ('application_entrypoint_ready', 'abandoned', false, 'Human stopped the employer application handoff.'),
  ('needs_account_auth', 'abandoned', false, 'Human stopped the application during employer authentication.'),
  ('needs_email_verification', 'abandoned', false, 'Human stopped the application during email verification.'),
  ('needs_mfa', 'abandoned', false, 'Human stopped the application during MFA.'),
  ('needs_human_checkpoint', 'abandoned', false, 'Human stopped the application during a manual checkpoint.'),
  ('application_form_ready', 'abandoned', false, 'Human stopped the application before deterministic autofill.')
ON CONFLICT (from_step, to_step) DO UPDATE
SET automated=EXCLUDED.automated, note=EXCLUDED.note;

CREATE TABLE IF NOT EXISTS document_revision_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  document_type text NOT NULL CHECK (document_type IN ('resume','cover_letter')),
  source_document_id uuid NOT NULL REFERENCES generated_documents(id) ON DELETE CASCADE,
  source_review_item_id uuid NOT NULL REFERENCES human_review_items(id) ON DELETE CASCADE,
  source_sha256 text NOT NULL CHECK (length(source_sha256)=64),
  feedback_text text NOT NULL CHECK (length(btrim(feedback_text)) BETWEEN 1 AND 8000),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  claimed_by text,
  lease_expires_at timestamptz,
  generated_document_id uuid REFERENCES generated_documents(id) ON DELETE SET NULL,
  error_message text,
  requested_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_revision_request_active
  ON document_revision_requests(source_review_item_id, source_sha256)
  WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_document_revision_request_queue
  ON document_revision_requests(status, created_at);

ALTER TABLE telegram_control_surface_state
  ADD COLUMN IF NOT EXISTS pending_document_review_item_id uuid REFERENCES human_review_items(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS pending_document_source_sha256 text,
  ADD COLUMN IF NOT EXISTS pending_document_feedback_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS pending_document_prompt_message_id bigint;

ALTER TABLE telegram_callback_tokens
  DROP CONSTRAINT IF EXISTS telegram_callback_tokens_action_check;
ALTER TABLE telegram_callback_tokens
  ADD CONSTRAINT telegram_callback_tokens_action_check
  CHECK (action IN ('approve','reject','revise','details','skip','answer','other','focus_browser','sensitive_confirm','document_feedback'));

COMMIT;
