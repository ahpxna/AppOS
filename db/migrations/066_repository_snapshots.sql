-- =========================================================
-- 066 -- Immutable GitHub repository snapshots and change sets
-- =========================================================
BEGIN;

CREATE TABLE IF NOT EXISTS repository_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_source_id uuid NOT NULL
    REFERENCES repository_evidence_sources(id) ON DELETE CASCADE,
  branch text NOT NULL,
  head_sha text NOT NULL,
  tree_sha text,
  parent_head_sha text,
  github_pushed_at timestamptz,
  observed_at timestamptz NOT NULL DEFAULT now(),
  analysis_status text NOT NULL DEFAULT 'pending'
    CHECK (analysis_status IN ('pending','unchanged','analyzing','analyzed','failed')),
  analysis_version text,
  analyzed_at timestamptz,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(repository_source_id, head_sha)
);

CREATE TABLE IF NOT EXISTS repository_change_sets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_source_id uuid NOT NULL
    REFERENCES repository_evidence_sources(id) ON DELETE CASCADE,
  base_snapshot_id uuid REFERENCES repository_snapshots(id) ON DELETE SET NULL,
  head_snapshot_id uuid NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
  changed_files jsonb NOT NULL DEFAULT '[]'::jsonb,
  change_classification jsonb NOT NULL DEFAULT '{}'::jsonb,
  requires_analysis boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(repository_source_id, head_snapshot_id)
);

ALTER TABLE repository_evidence_sources
  ADD COLUMN IF NOT EXISTS current_snapshot_id uuid
    REFERENCES repository_snapshots(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS last_analyzed_snapshot_id uuid
    REFERENCES repository_snapshots(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS freshness_status text NOT NULL DEFAULT 'unobserved'
    CHECK (freshness_status IN ('unobserved','fresh','changed','stale','unavailable','excluded')),
  ADD COLUMN IF NOT EXISTS last_refresh_attempt_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_refresh_error text;

CREATE INDEX IF NOT EXISTS idx_repository_snapshots_source_observed
  ON repository_snapshots(repository_source_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_repository_change_sets_source
  ON repository_change_sets(repository_source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_repository_sources_freshness
  ON repository_evidence_sources(freshness_status, last_seen_at DESC);

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('repository_snapshot_tracker', 'service', 'profile',
   'Pin each configured GitHub project to an immutable branch HEAD and classify only the files changed since the last analyzed snapshot.',
   false, 'active',
   'Daily polling is convenience; document generation performs a freshness preflight against current GitHub HEAD.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
