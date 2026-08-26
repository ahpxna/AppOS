-- 077 -- Daily control surface: progressive review, batch-safe decisions, snooze, and dashboard state.
BEGIN;

ALTER TABLE human_review_items
  ADD COLUMN IF NOT EXISTS snoozed_until timestamptz;
CREATE INDEX IF NOT EXISTS idx_human_review_items_snoozed
  ON human_review_items(snoozed_until)
  WHERE snoozed_until IS NOT NULL;

ALTER TABLE telegram_callback_tokens
  DROP CONSTRAINT IF EXISTS telegram_callback_tokens_action_check;
ALTER TABLE telegram_callback_tokens
  ADD CONSTRAINT telegram_callback_tokens_action_check
  CHECK (action IN ('approve','reject','revise','details','skip','answer','other'));
ALTER TABLE telegram_callback_tokens
  ADD COLUMN IF NOT EXISTS payload_json jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS telegram_ui_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_sha256 text NOT NULL UNIQUE,
  action text NOT NULL CHECK (action IN ('review_next','approve_safe','refresh')),
  allowed_user_id bigint NOT NULL,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_telegram_ui_tokens_live
  ON telegram_ui_tokens(expires_at)
  WHERE used_at IS NULL;

CREATE TABLE IF NOT EXISTS telegram_control_surface_state (
  chat_id bigint PRIMARY KEY,
  dashboard_message_id bigint,
  last_digest text,
  pending_question_review_item_id uuid REFERENCES human_review_items(id) ON DELETE SET NULL,
  pending_question_source_sha256 text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW v_human_review_inbox AS
SELECT
  hri.id AS review_item_id,
  hri.review_bundle_id,
  hri.application_id,
  hri.item_type,
  hri.status,
  hri.priority,
  hri.title,
  hri.summary_text,
  hri.generated_document_id,
  hri.approval_request_id,
  hri.browser_task_id,
  hri.payload_json,
  hri.created_at,
  a.company,
  a.job_title,
  a.job_url
FROM human_review_items hri
JOIN applications a ON a.id = hri.application_id
WHERE hri.status IN ('pending','needs_revision')
  AND (hri.snoozed_until IS NULL OR hri.snoozed_until <= now())
ORDER BY CASE hri.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
         hri.created_at;

COMMIT;
