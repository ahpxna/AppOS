-- 051 -- scalable employer immigration evidence and approval reissue safety
--
-- The migration ledger itself is created by scripts/apply_migrations.py before
-- any migration runs, so it can atomically record this file like every other
-- migration. This SQL keeps the data model portion portable for direct psql
-- inspection too.

BEGIN;

ALTER TABLE immigration_profiles
  ADD COLUMN IF NOT EXISTS us_citizen text NOT NULL DEFAULT 'unconfirmed',
  ADD COLUMN IF NOT EXISTS us_person text NOT NULL DEFAULT 'unconfirmed',
  ADD COLUMN IF NOT EXISTS permanent_work_authorization text NOT NULL DEFAULT 'unconfirmed';

ALTER TABLE immigration_profiles
  DROP CONSTRAINT IF EXISTS chk_immigration_us_citizen,
  ADD CONSTRAINT chk_immigration_us_citizen
    CHECK (us_citizen IN ('yes', 'no', 'unconfirmed')),
  DROP CONSTRAINT IF EXISTS chk_immigration_us_person,
  ADD CONSTRAINT chk_immigration_us_person
    CHECK (us_person IN ('yes', 'no', 'unconfirmed')),
  DROP CONSTRAINT IF EXISTS chk_immigration_permanent_work_authorization,
  ADD CONSTRAINT chk_immigration_permanent_work_authorization
    CHECK (permanent_work_authorization IN ('yes', 'no', 'unconfirmed'));

ALTER TABLE application_immigration_assessments
  ADD COLUMN IF NOT EXISTS restriction_type text NOT NULL DEFAULT 'UNKNOWN';

ALTER TABLE application_immigration_assessments
  DROP CONSTRAINT IF EXISTS chk_immigration_restriction_type,
  ADD CONSTRAINT chk_immigration_restriction_type
    CHECK (restriction_type IN (
      'NO_SPONSORSHIP', 'PERMANENT_AUTHORIZATION', 'US_CITIZENSHIP',
      'US_PERSON', 'OPT_COMPATIBLE', 'STEM_OPT_COMPATIBLE', 'UNKNOWN'
    ));

CREATE TABLE IF NOT EXISTS employers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name text NOT NULL,
  normalized_name text NOT NULL UNIQUE,
  legal_entity_name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_employers_updated_at ON employers;
CREATE TRIGGER trg_employers_updated_at
BEFORE UPDATE ON employers
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS employer_aliases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employer_id uuid NOT NULL REFERENCES employers(id) ON DELETE CASCADE,
  alias_name text NOT NULL,
  normalized_alias text NOT NULL UNIQUE,
  source text NOT NULL DEFAULT 'application_intake',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_employer_aliases_employer
  ON employer_aliases(employer_id);

CREATE TABLE IF NOT EXISTS employer_immigration_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employer_id uuid NOT NULL REFERENCES employers(id) ON DELETE CASCADE,
  evidence_type text NOT NULL,
  status text NOT NULL,
  source_name text NOT NULL,
  source_url text NOT NULL,
  legal_entity_name text,
  confidence numeric NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  observed_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  notes text,
  recorded_by text NOT NULL DEFAULT 'user',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chk_employer_immigration_evidence_type
    CHECK (evidence_type IN ('everify', 'h1b_history')),
  CONSTRAINT chk_employer_immigration_evidence_status
    CHECK (
      (evidence_type = 'everify' AND status IN ('verified', 'not_found', 'unknown'))
      OR (evidence_type = 'h1b_history' AND status IN ('positive', 'none_found', 'unknown'))
    )
);

CREATE INDEX IF NOT EXISTS idx_employer_immigration_evidence_latest
  ON employer_immigration_evidence(employer_id, evidence_type, observed_at DESC);

ALTER TABLE applications
  ADD COLUMN IF NOT EXISTS employer_id uuid REFERENCES employers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_applications_employer_id ON applications(employer_id);

-- Retire the permanent idempotency lock. Only a currently pending/approved
-- request represents a live capability; expired, denied, and consumed rows
-- remain auditable but must not prevent a new request for unchanged content.
DROP INDEX IF EXISTS idx_approval_requests_idempotency_key;
CREATE UNIQUE INDEX idx_approval_requests_idempotency_key_active
  ON approval_requests(idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status IN ('pending', 'approved');

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('employer_immigration_evidence', 'service', 'L0',
   'Reuse sourced E-Verify and H-1B-history evidence across an employer''s jobs.',
   false, 'active',
   'Employer evidence is separate from job policy; E-Verify is not a sponsorship guarantee.', now(), now()),
  ('immigration_fit_synthesis', 'safety', 'L1',
   'Combine candidate-confirmed profile, job restriction semantics, and employer evidence into an explainable rank.',
   false, 'active',
   'No automatic legal form answers. UNKNOWN remains distinct from positive evidence.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status,
    notes = EXCLUDED.notes, updated_at = now();

COMMIT;
