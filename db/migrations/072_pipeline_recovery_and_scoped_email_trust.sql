BEGIN;

CREATE TABLE IF NOT EXISTS application_scoped_domain_trusts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id uuid NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  domain text NOT NULL,
  purpose text NOT NULL,
  expires_at timestamptz NOT NULL,
  approval_request_id uuid REFERENCES approval_requests(id) ON DELETE SET NULL,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_application_scoped_domain_trust
  ON application_scoped_domain_trusts(application_id, domain, purpose);
CREATE INDEX IF NOT EXISTS idx_application_scoped_domain_trust_expiry
  ON application_scoped_domain_trusts(expires_at) WHERE enabled=true;

INSERT INTO pipeline_transitions (from_step, to_step, automated, note)
VALUES
  ('application_ready', 'needs_account_auth', false, 'A later application wizard page requires employer account authentication.'),
  ('application_ready', 'needs_email_verification', false, 'A later application wizard page requires employer email verification.'),
  ('application_ready', 'needs_mfa', false, 'A later application wizard page requires MFA.'),
  ('application_ready', 'needs_human_checkpoint', false, 'A later application wizard page requires a manual checkpoint.')
ON CONFLICT (from_step, to_step) DO UPDATE SET automated=EXCLUDED.automated, note=EXCLUDED.note;

COMMIT;
