-- 071 -- Human Approval Bus + privileged application actions + Gmail verification + credential vault
BEGIN;

CREATE TABLE IF NOT EXISTS approval_context_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id uuid REFERENCES human_review_items(id) ON DELETE CASCADE,
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  action_scope text NOT NULL,
  context_sha256 text NOT NULL,
  context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  diff_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approval_context_snapshots_scope
  ON approval_context_snapshots(application_id, action_scope, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_context_snapshots_review
  ON approval_context_snapshots(review_item_id, created_at DESC);

ALTER TABLE telegram_review_deliveries
  ADD COLUMN IF NOT EXISTS context_sha256 text;
CREATE INDEX IF NOT EXISTS idx_telegram_review_deliveries_context
  ON telegram_review_deliveries(review_item_id, chat_id, delivery_kind, context_sha256)
  WHERE context_sha256 IS NOT NULL AND status = 'sent';

CREATE TABLE IF NOT EXISTS credential_vault_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  origin text NOT NULL,
  account_key text NOT NULL,
  secret_kind text NOT NULL,
  ciphertext bytea NOT NULL,
  nonce bytea NOT NULL,
  aad text NOT NULL,
  secret_sha256 text NOT NULL,
  key_version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','rotated','revoked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  rotated_at timestamptz,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_credential_vault_active
  ON credential_vault_entries(origin, account_key, secret_kind)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_credential_vault_lookup
  ON credential_vault_entries(origin, account_key, secret_kind, created_at DESC);

CREATE TABLE IF NOT EXISTS application_auth_sessions (
  application_id uuid PRIMARY KEY REFERENCES applications(id) ON DELETE CASCADE,
  employer_origin text,
  account_email text,
  platform_hint text,
  auth_state text NOT NULL DEFAULT 'unknown' CHECK (auth_state IN (
    'unknown','application_form_ready','needs_account_auth','needs_email_verification',
    'needs_mfa','needs_human_checkpoint','needs_manual_sso','authenticated','failed'
  )),
  current_url text,
  page_fingerprint text,
  last_event text,
  detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS email_verification_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  gmail_account text NOT NULL,
  gmail_message_id text NOT NULL,
  sender text,
  subject text,
  received_at timestamptz,
  verification_kind text NOT NULL CHECK (verification_kind IN ('numeric_code','magic_link')),
  secret_sha256 text NOT NULL,
  secret_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'discovered' CHECK (status IN ('discovered','approved','consumed','expired','rejected')),
  created_at timestamptz NOT NULL DEFAULT now(),
  consumed_at timestamptz,
  UNIQUE (application_id, gmail_message_id, verification_kind, secret_sha256)
);
CREATE INDEX IF NOT EXISTS idx_email_verification_candidates_app
  ON email_verification_candidates(application_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS privileged_action_executions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  approval_request_id uuid NOT NULL UNIQUE REFERENCES approval_requests(id) ON DELETE RESTRICT,
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  action_type text NOT NULL,
  status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','needs_reconciliation')),
  target_id text,
  expected_url text,
  observed_url text,
  expected_page_fingerprint text,
  observed_page_fingerprint text,
  result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_privileged_action_executions_app
  ON privileged_action_executions(application_id, started_at DESC);

INSERT INTO pipeline_steps (step, layer, description, is_terminal, requires_human, sort_order)
VALUES
  ('application_entrypoint_ready', 'L3', 'Human-approved application handoff is ready to open.', false, true, 82),
  ('needs_account_auth', 'L3', 'Employer account login or registration requires a separate human approval.', false, true, 84),
  ('needs_email_verification', 'L3', 'Employer account is waiting for a bounded Gmail verification approval.', false, true, 85),
  ('needs_mfa', 'L3', 'Non-email MFA requires a separate human action and retry approval.', false, true, 86),
  ('needs_human_checkpoint', 'L3', 'CAPTCHA/bot/risk checkpoint requires manual handling before a fresh retry.', false, true, 87),
  ('application_form_ready', 'L7', 'Application form target is authenticated/ready for deterministic autofill.', false, false, 88)
ON CONFLICT (step) DO UPDATE
SET description = EXCLUDED.description, requires_human = EXCLUDED.requires_human, sort_order = EXCLUDED.sort_order;

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('docs_verified', 'application_entrypoint_ready', false, 'Human approved opening the application entry point.'),
  ('application_entrypoint_ready', 'needs_account_auth', false, 'Employer site requires account authentication.'),
  ('application_entrypoint_ready', 'needs_email_verification', false, 'Employer site requires email verification.'),
  ('application_entrypoint_ready', 'needs_mfa', false, 'Employer site requires MFA.'),
  ('application_entrypoint_ready', 'needs_human_checkpoint', false, 'Employer site requires a manual human checkpoint.'),
  ('application_entrypoint_ready', 'application_form_ready', false, 'Application form is directly available.'),
  ('needs_account_auth', 'needs_email_verification', false, 'Account flow advanced to email verification.'),
  ('needs_account_auth', 'needs_mfa', false, 'Account flow advanced to MFA.'),
  ('needs_account_auth', 'needs_human_checkpoint', false, 'Account flow advanced to a manual checkpoint.'),
  ('needs_account_auth', 'application_form_ready', false, 'Employer account is ready and the form is available.'),
  ('needs_email_verification', 'needs_mfa', false, 'Email verification advanced to another MFA factor.'),
  ('needs_email_verification', 'needs_human_checkpoint', false, 'Email verification advanced to a manual checkpoint.'),
  ('needs_email_verification', 'application_form_ready', false, 'Email verification completed and the form is available.'),
  ('needs_mfa', 'needs_human_checkpoint', false, 'MFA advanced to a manual checkpoint.'),
  ('needs_mfa', 'application_form_ready', false, 'MFA completed and the form is available.'),
  ('needs_human_checkpoint', 'application_form_ready', false, 'Human checkpoint completed and the form is available.'),
  ('application_form_ready', 'awaiting_approval', false, 'Exact current form was packaged for autofill approval.'),
  ('application_ready', 'application_form_ready', false, 'A human-approved multi-page advance revealed another application form step.')
ON CONFLICT (from_step, to_step) DO UPDATE SET automated=EXCLUDED.automated, note=EXCLUDED.note;

INSERT INTO ats_capabilities
  (ats_type, supports_discovery, supports_static_text, supports_radio, supports_select, supports_upload, supports_multi_page, autofill_mode, notes)
VALUES
  ('linkedin_easy_apply', false, true, true, true, true, true, 'single_page',
   'Easy Apply is handled one visible page at a time: exact autofill approval per page, separate human-approved Next/Review transitions, and privileged final Submit.')
ON CONFLICT (ats_type) DO UPDATE
SET supports_static_text=EXCLUDED.supports_static_text, supports_radio=EXCLUDED.supports_radio,
    supports_select=EXCLUDED.supports_select, supports_upload=EXCLUDED.supports_upload,
    supports_multi_page=EXCLUDED.supports_multi_page, autofill_mode=EXCLUDED.autofill_mode,
    notes=EXCLUDED.notes, updated_at=now();

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('human_approval_bus_v2', 'service', 'L1',
   'Build soft-fail full-context approval envelopes, context diffs, and Telegram review packages for every human gate.',
   false, 'active',
   'Missing context renders as NaN and never suppresses delivery/approval controls; privileged executors still fail closed on exact pre-I/O invariants.', now(), now()),
  ('credential_vault', 'security', 'L1',
   'Encrypt employer-account credentials with AES-256-GCM under a local master key; plaintext secrets are never stored in PostgreSQL or Telegram.',
   false, 'active',
   'Only active entries are unique; rotations preserve ciphertext history without exposing prior plaintext.', now(), now()),
  ('gmail_verification_reader', 'service', 'L3',
   'Bounded read-only Gmail verification lookup through gog/OpenClaw Google tooling, including explicit Spam search.',
   false, 'active',
   'OTP/magic-link plaintext is used only in memory; DB stores message metadata and SHA-256 only.', now(), now()),
  ('privileged_application_executor', 'service', 'L3',
   'Execute exact one-shot browser actions only after a human-approved Telegram capability and fresh page revalidation.',
   false, 'active',
   'Final Submit, account/auth, consent and application handoff are separate capabilities; no approval is transferable.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
