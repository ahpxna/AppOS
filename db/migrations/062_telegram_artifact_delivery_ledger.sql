-- 062 -- Track exact Telegram review artifacts independently from summaries.
BEGIN;

ALTER TABLE telegram_review_deliveries
  ADD COLUMN IF NOT EXISTS artifact_sha256 text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_review_artifact_delivery
  ON telegram_review_deliveries(review_item_id, chat_id, delivery_kind, artifact_sha256)
  WHERE delivery_kind = 'artifact' AND artifact_sha256 IS NOT NULL AND status = 'sent';

COMMIT;
