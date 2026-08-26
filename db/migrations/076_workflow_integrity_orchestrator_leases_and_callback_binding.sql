-- 076 -- Workflow integrity: one docs->apply path, orchestrator leases,
-- version-bound Telegram callbacks, and LLM budget accounting metadata.
BEGIN;

-- The modern application-entry flow is the only authoritative path after QA.
DELETE FROM pipeline_transitions
 WHERE from_step='docs_verified' AND to_step='awaiting_approval';

UPDATE pipeline_steps
   SET requires_human=true,
       description='Documents passed QA; exact reviewed resume approval and OPEN APPLY handoff are human-gated.'
 WHERE step='docs_verified';

-- Long-running orchestration must claim an application before model/network/file
-- work. The lease is committed before subprocess work and completion checks the
-- same run id, preventing duplicate LLM/document generation by two orchestrators.
ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS processing_run_id uuid,
  ADD COLUMN IF NOT EXISTS processing_step text,
  ADD COLUMN IF NOT EXISTS processing_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS processing_lease_expires_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_applications_processing_lease
  ON applications(processing_lease_expires_at)
  WHERE processing_run_id IS NOT NULL;

-- A Telegram button is authorization for the exact review source/context that
-- was rendered into its message, never a mutable review-item id by itself.
ALTER TABLE telegram_callback_tokens
  ADD COLUMN IF NOT EXISTS source_sha256 text,
  ADD COLUMN IF NOT EXISTS context_sha256 text;
CREATE INDEX IF NOT EXISTS idx_telegram_callback_tokens_review_source
  ON telegram_callback_tokens(review_item_id, source_sha256, context_sha256)
  WHERE used_at IS NULL;

-- Per-call accounting metadata. Existing ledger rows remain valid.
ALTER TABLE cost_ledger
  ADD COLUMN IF NOT EXISTS provider text,
  ADD COLUMN IF NOT EXISTS provider_request_id text,
  ADD COLUMN IF NOT EXISTS resolved_model_name text;

CREATE TABLE IF NOT EXISTS llm_cost_reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  role text NOT NULL,
  provider text NOT NULL,
  model_name text NOT NULL,
  reserved_cost_usd numeric NOT NULL CHECK (reserved_cost_usd >= 0),
  status text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','settled','released','uncertain')),
  created_at timestamptz NOT NULL DEFAULT now(),
  settled_at timestamptz,
  detail_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_llm_cost_reservations_open
  ON llm_cost_reservations(status, created_at)
  WHERE status IN ('reserved','uncertain');

COMMIT;
