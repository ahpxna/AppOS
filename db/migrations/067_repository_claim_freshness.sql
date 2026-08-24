-- =========================================================
-- 067 -- Claim-level repository freshness and observations
-- =========================================================
BEGIN;

CREATE TABLE IF NOT EXISTS repository_claims (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_source_id uuid NOT NULL
    REFERENCES repository_evidence_sources(id) ON DELETE CASCADE,
  project_id text NOT NULL,
  claim_key text NOT NULL,
  claim_kind text NOT NULL,
  claim_text text NOT NULL,
  current_snapshot_id uuid REFERENCES repository_snapshots(id) ON DELETE SET NULL,
  evidence_path text,
  evidence_blob_sha text,
  source_line_start integer,
  source_line_end integer,
  github_authority numeric NOT NULL DEFAULT 0.70,
  document_authority numeric NOT NULL DEFAULT 0.30,
  confidence numeric NOT NULL DEFAULT 0.70,
  freshness_status text NOT NULL DEFAULT 'fresh'
    CHECK (freshness_status IN (
      'fresh','affected','revalidated','contradicted','source_missing','superseded'
    )),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(repository_source_id, project_id, claim_key)
);

CREATE TABLE IF NOT EXISTS repository_claim_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_claim_id uuid NOT NULL REFERENCES repository_claims(id) ON DELETE CASCADE,
  repository_snapshot_id uuid NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
  claim_text text NOT NULL,
  evidence_path text,
  evidence_blob_sha text,
  source_line_start integer,
  source_line_end integer,
  observation_status text NOT NULL DEFAULT 'observed'
    CHECK (observation_status IN ('observed','revalidated','contradicted','source_missing')),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  observed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(repository_claim_id, repository_snapshot_id)
);

CREATE TABLE IF NOT EXISTS project_source_conflicts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id text NOT NULL,
  claim_key text NOT NULL,
  github_claim_id uuid REFERENCES repository_claims(id) ON DELETE SET NULL,
  document_asset_id uuid REFERENCES profile_assets(id) ON DELETE SET NULL,
  github_value text,
  document_value text,
  resolution text
    CHECK (resolution IN ('github','document','user','not_a_conflict')),
  status text NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','resolved','superseded')),
  resolution_note text,
  resolved_by text,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(project_id, claim_key, github_claim_id, document_asset_id)
);

CREATE INDEX IF NOT EXISTS idx_repository_claims_project_freshness
  ON repository_claims(project_id, freshness_status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_repository_claims_evidence_path
  ON repository_claims(repository_source_id, evidence_path);
CREATE INDEX IF NOT EXISTS idx_project_source_conflicts_open
  ON project_source_conflicts(project_id, status);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('repository_claim_freshness', 'service', 'profile',
   'Track project implementation claims at file/line granularity so a GitHub commit invalidates only claims supported by changed evidence.',
   false, 'active',
   'Implementation facts prefer current GitHub evidence; ownership, dates and other personal assertions still require user/official evidence.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
