-- 054 -- durable state before irreversible browser side effects
BEGIN;

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS executing_task_id uuid REFERENCES browser_tasks(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS bound_artifact_id uuid REFERENCES generated_document_artifacts(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS bound_artifact_sha256 text,
  ADD COLUMN IF NOT EXISTS bound_artifact_filename text;

ALTER TABLE approval_requests
  DROP CONSTRAINT IF EXISTS chk_approval_single_use;
ALTER TABLE approval_requests
  ADD CONSTRAINT chk_approval_single_use
  CHECK (
    (status IN ('pending', 'approved', 'denied', 'expired') AND consumed_at IS NULL)
    OR (status = 'executing' AND executing_task_id IS NOT NULL AND consumed_at IS NULL)
    OR (status = 'consumed' AND consumed_at IS NOT NULL)
  );

-- An executing capability is still live, even if the worker subsequently
-- crashes.  Do not let the same idempotency key create a second browser task
-- until the first one has been reconciled/consumed.
DROP INDEX IF EXISTS idx_approval_requests_idempotency_key_active;
CREATE UNIQUE INDEX idx_approval_requests_idempotency_key_active
  ON approval_requests(idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND status IN ('pending', 'approved', 'executing');

ALTER TABLE browser_tasks
  ADD COLUMN IF NOT EXISTS bound_artifact_id uuid REFERENCES generated_document_artifacts(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS artifact_sha256 text,
  ADD COLUMN IF NOT EXISTS artifact_filename text,
  ADD COLUMN IF NOT EXISTS pinned_target_id text,
  ADD COLUMN IF NOT EXISTS execution_state text NOT NULL DEFAULT 'not_started'
    CHECK (execution_state IN ('not_started', 'executing', 'partial', 'completed', 'needs_reconciliation'));

CREATE TABLE IF NOT EXISTS autofill_action_journal (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  browser_task_id uuid NOT NULL REFERENCES browser_tasks(id) ON DELETE CASCADE,
  approval_request_id uuid NOT NULL REFERENCES approval_requests(id) ON DELETE RESTRICT,
  sequence_no integer NOT NULL,
  target_id text NOT NULL,
  action_kind text NOT NULL CHECK (action_kind IN ('fill', 'select', 'check', 'upload')),
  target_ref text NOT NULL,
  expected_value_sha256 text NOT NULL,
  status text NOT NULL CHECK (status IN ('started', 'verified', 'failed')),
  observed_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  verified_at timestamptz,
  UNIQUE (browser_task_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_autofill_action_journal_task_status
  ON autofill_action_journal(browser_task_id, status, sequence_no);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('autofill_execution_journal', 'safety', 'L7',
   'Persist an executing capability and every external browser action before it runs.',
   false, 'active',
   'A crash after a browser side effect requires reconciliation; JobOS never replays an executing task automatically.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
