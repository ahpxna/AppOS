-- =========================================================
-- 065 -- User-verified fixed resume fields and certification lifecycle
-- =========================================================
BEGIN;

CREATE TABLE IF NOT EXISTS candidate_fixed_fields (
  field_key text PRIMARY KEY,
  field_group text NOT NULL,
  value_json jsonb NOT NULL DEFAULT 'null'::jsonb,
  display_value text,
  mode text NOT NULL DEFAULT 'fixed'
    CHECK (mode IN ('fixed','derived','dynamic')),
  verification_status text NOT NULL DEFAULT 'missing'
    CHECK (verification_status IN (
      'missing','candidate','document_verified','user_verified','conflict','expired','excluded'
    )),
  verified_by text,
  verified_at timestamptz,
  source_revision_id uuid REFERENCES profile_source_revisions(id) ON DELETE SET NULL,
  expires_at timestamptz,
  show_on_resume boolean NOT NULL DEFAULT true,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS candidate_fixed_field_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  field_key text NOT NULL,
  value_json jsonb NOT NULL,
  verification_status text NOT NULL,
  changed_by text,
  source_revision_id uuid REFERENCES profile_source_revisions(id) ON DELETE SET NULL,
  changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_candidate_fixed_field_history_key
  ON candidate_fixed_field_history(field_key, changed_at DESC);

-- Parsed official documents may suggest a value, but a suggestion never becomes
-- canonical truth until the user accepts it.  Keeping suggestions versioned by
-- source revision lets a new transcript/CV surface a conflict without silently
-- overwriting a previously verified GPA/contact/education value.
CREATE TABLE IF NOT EXISTS candidate_fixed_field_suggestions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  field_key text NOT NULL,
  suggested_value_json jsonb NOT NULL,
  suggested_display_value text NOT NULL,
  source_revision_id uuid NOT NULL REFERENCES profile_source_revisions(id) ON DELETE CASCADE,
  confidence numeric NOT NULL DEFAULT 0.70,
  conflicts_current boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','accepted','rejected','superseded')),
  extractor_version text NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(field_key, source_revision_id, suggested_display_value)
);

CREATE INDEX IF NOT EXISTS idx_candidate_fixed_suggestions_pending
  ON candidate_fixed_field_suggestions(field_key, status, conflicts_current);

CREATE TABLE IF NOT EXISTS candidate_certifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  issuer text,
  certification_status text NOT NULL DEFAULT 'planned'
    CHECK (certification_status IN ('planned','studying','scheduled','earned','expired','revoked','excluded')),
  earned_at date,
  expires_at date,
  credential_id text,
  credential_url text,
  show_on_resume boolean NOT NULL DEFAULT false,
  verification_status text NOT NULL DEFAULT 'candidate'
    CHECK (verification_status IN ('candidate','document_verified','user_verified','conflict','expired','excluded')),
  source_revision_id uuid REFERENCES profile_source_revisions(id) ON DELETE SET NULL,
  verified_by text,
  verified_at timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(name, issuer)
);

INSERT INTO applicant_identity (field_name, field_value, field_group, approved, notes)
VALUES
  ('gpa', 'FILL_ME', 'education', false, 'User-verified GPA value when the candidate chooses to disclose it.'),
  ('gpa_scale', 'FILL_ME', 'education', false, 'Scale corresponding to GPA, for example 4.0 or 10.0.')
ON CONFLICT (field_name) DO NOTHING;

CREATE OR REPLACE VIEW v_candidate_fixed_resume_readiness AS
SELECT
  count(*) FILTER (WHERE verification_status IN ('user_verified','document_verified')) AS verified_fields,
  count(*) FILTER (WHERE verification_status IN ('missing','candidate','conflict','expired')) AS unresolved_fields,
  count(*) FILTER (
    WHERE show_on_resume = true
      AND verification_status NOT IN ('user_verified','document_verified')
  ) AS unresolved_visible_fields
FROM candidate_fixed_fields;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('fixed_resume_field_registry', 'service', 'profile',
   'Hold user-verified resume identity, education, GPA-display choices and certification lifecycle separately from dynamic project evidence.',
   false, 'active',
   'LLMs may suggest candidate values but cannot silently overwrite user-verified fixed fields.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
