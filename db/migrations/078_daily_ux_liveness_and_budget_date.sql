-- 078 -- Daily UX liveness: sensitive-question cards, bounded natural replies,
-- callback invalidation, and immutable paid-budget ownership.
BEGIN;

ALTER TABLE human_review_items
  DROP CONSTRAINT IF EXISTS human_review_items_item_type_check;
ALTER TABLE human_review_items
  ADD CONSTRAINT human_review_items_item_type_check CHECK (item_type IN (
    'document_review','approval_request','autofill_review','question_required',
    'sensitive_question_required','reconciliation_required','application_ready','action_required'
  ));
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_sensitive_question_active
  ON human_review_items(application_id, source_sha256)
  WHERE source_sha256 IS NOT NULL AND item_type='sensitive_question_required'
    AND status IN ('pending','needs_revision');

ALTER TABLE telegram_callback_tokens
  DROP CONSTRAINT IF EXISTS telegram_callback_tokens_action_check;
ALTER TABLE telegram_callback_tokens
  ADD CONSTRAINT telegram_callback_tokens_action_check
  CHECK (action IN ('approve','reject','revise','details','skip','answer','other','focus_browser','sensitive_confirm'));

ALTER TABLE telegram_control_surface_state
  ADD COLUMN IF NOT EXISTS pending_question_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS pending_question_prompt_message_id bigint;

ALTER TABLE llm_cost_reservations
  ADD COLUMN IF NOT EXISTS budget_date date;
UPDATE llm_cost_reservations
   SET budget_date = COALESCE(budget_date, created_at::date)
 WHERE budget_date IS NULL;
ALTER TABLE llm_cost_reservations
  ALTER COLUMN budget_date SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_cost_reservations_budget_date
  ON llm_cost_reservations(budget_date, status);

ALTER TABLE cost_ledger
  ADD COLUMN IF NOT EXISTS budget_date date;
UPDATE cost_ledger
   SET budget_date = COALESCE(budget_date, created_at::date)
 WHERE budget_date IS NULL;
CREATE INDEX IF NOT EXISTS idx_cost_ledger_budget_date
  ON cost_ledger(budget_date);

COMMIT;
