-- 092 -- Durable Telegram inbox/outbox semantics around the external API boundary.
BEGIN;

ALTER TABLE telegram_review_deliveries
  DROP CONSTRAINT IF EXISTS telegram_review_deliveries_status_check;
ALTER TABLE telegram_review_deliveries
  ADD CONSTRAINT telegram_review_deliveries_status_check
  CHECK (status IN ('pending','sending','sent','failed','uncertain'));
ALTER TABLE telegram_review_deliveries
  ALTER COLUMN delivered_at DROP NOT NULL,
  ALTER COLUMN delivered_at DROP DEFAULT;
ALTER TABLE telegram_review_deliveries
  ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE telegram_review_deliveries
  ADD COLUMN IF NOT EXISTS dedupe_key text,
  ADD COLUMN IF NOT EXISTS method text,
  ADD COLUMN IF NOT EXISTS payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS request_sha256 text,
  ADD COLUMN IF NOT EXISTS payload_redaction_version integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS claimed_by text,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
UPDATE telegram_review_deliveries
   SET request_sha256=encode(digest(payload_json::text,'sha256'),'hex')
 WHERE request_sha256 IS NULL;
ALTER TABLE telegram_review_deliveries
  ALTER COLUMN request_sha256 SET NOT NULL;
ALTER TABLE telegram_review_deliveries DROP CONSTRAINT IF EXISTS telegram_review_deliveries_request_sha256_check;
ALTER TABLE telegram_review_deliveries ADD CONSTRAINT telegram_review_deliveries_request_sha256_check
  CHECK (length(request_sha256)=64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_review_delivery_dedupe
  ON telegram_review_deliveries(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_telegram_review_delivery_outbox
  ON telegram_review_deliveries(status,lease_expires_at,delivered_at)
  WHERE status IN ('pending','sending','uncertain');

CREATE TABLE IF NOT EXISTS telegram_delivery_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  delivery_id uuid NOT NULL REFERENCES telegram_review_deliveries(id) ON DELETE CASCADE,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  status text NOT NULL CHECK (status IN ('started','sent','failed','uncertain')),
  telegram_message_id bigint,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE(delivery_id,attempt_no)
);

CREATE TABLE IF NOT EXISTS telegram_updates (
  bot_key text NOT NULL,
  update_id bigint NOT NULL,
  payload_sha256 text NOT NULL CHECK (length(payload_sha256)=64),
  payload_json jsonb NOT NULL,
  payload_redaction_version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','processing','processed','failed','uncertain')),
  claimed_by text,
  lease_expires_at timestamptz,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  error_message text,
  PRIMARY KEY(bot_key,update_id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_unprocessed
  ON telegram_updates(status,lease_expires_at,update_id)
  WHERE status IN ('received','processing','failed');

COMMIT;
