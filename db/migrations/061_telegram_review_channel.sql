-- 061 -- Telegram as a remote UI adapter for the Human Review Hub
BEGIN;

CREATE TABLE IF NOT EXISTS telegram_review_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id uuid NOT NULL REFERENCES human_review_items(id) ON DELETE CASCADE,
  chat_id bigint NOT NULL,
  message_id bigint,
  delivery_kind text NOT NULL CHECK (delivery_kind IN ('summary','artifact','decision_update')),
  status text NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','failed')),
  error_message text,
  delivered_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_item_id, chat_id, delivery_kind, message_id)
);

CREATE TABLE IF NOT EXISTS telegram_callback_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id uuid NOT NULL REFERENCES human_review_items(id) ON DELETE CASCADE,
  token_sha256 text NOT NULL UNIQUE,
  action text NOT NULL CHECK (action IN ('approve','reject','revise')),
  allowed_user_id bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_telegram_callback_tokens_live
  ON telegram_callback_tokens(expires_at) WHERE used_at IS NULL;

CREATE TABLE IF NOT EXISTS telegram_bot_state (
  bot_key text PRIMARY KEY,
  update_offset bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO telegram_bot_state(bot_key, update_offset) VALUES ('review_bot', 0)
ON CONFLICT (bot_key) DO NOTHING;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('telegram_review_channel', 'adapter', 'L1',
   'Deliver review PDFs/screenshots and collect authenticated review decisions through Telegram long polling.',
   false, 'active',
   'Outbound-only network posture; callback data contains opaque single-use tokens. Telegram never directly mutates approval capability rows.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
