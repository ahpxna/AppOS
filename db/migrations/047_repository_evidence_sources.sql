-- =========================================================
-- 047 -- Repository evidence as an independent profile source
--
-- GitHub metadata and isolated test output are not profile chunks. They are
-- retained separately, require an explicit user ownership confirmation, and
-- can only reach L6 after a reviewed profile_asset is approved.
-- =========================================================

BEGIN;

CREATE TABLE IF NOT EXISTS repository_evidence_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  provider text NOT NULL DEFAULT 'github'
    CHECK (provider IN ('github', 'gitlab', 'local_git')),
  repo_full_name text NOT NULL,
  canonical_url text NOT NULL,
  clone_url text,
  default_branch text,
  revision_sha text,

  is_private boolean NOT NULL DEFAULT false,
  is_fork boolean NOT NULL DEFAULT false,
  archived boolean NOT NULL DEFAULT false,
  description text,
  homepage text,
  primary_language text,
  topics text[] NOT NULL DEFAULT '{}'::text[],
  source_payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- GitHub visibility/collaboration is not proof the candidate authored code.
  -- This field is set only by the explicit confirm-ownership command.
  ownership_status text NOT NULL DEFAULT 'unconfirmed'
    CHECK (ownership_status IN ('unconfirmed', 'confirmed_by_user', 'excluded')),
  ownership_confirmed_at timestamptz,
  ownership_confirmed_by text,
  status text NOT NULL DEFAULT 'discovered'
    CHECK (status IN ('discovered', 'ownership_confirmed', 'excluded')),

  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (provider, repo_full_name)
);

CREATE INDEX IF NOT EXISTS idx_repository_evidence_sources_review
  ON repository_evidence_sources(status, ownership_status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS repository_evidence_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  repository_source_id uuid NOT NULL
    REFERENCES repository_evidence_sources(id) ON DELETE CASCADE,

  evidence_type text NOT NULL
    CHECK (evidence_type IN (
      'repository_metadata', 'primary_language', 'topic', 'audit_check',
      'audit_finding', 'user_ownership_confirmation'
    )),
  evidence_key text NOT NULL,
  evidence_text text NOT NULL,
  source_url text,
  source_path text,
  source_line_start integer,
  source_line_end integer,
  evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'excluded')),
  created_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (repository_source_id, evidence_type, evidence_key)
);

CREATE INDEX IF NOT EXISTS idx_repository_evidence_items_source
  ON repository_evidence_items(repository_source_id, status, evidence_type);

CREATE TABLE IF NOT EXISTS repository_evidence_asset_links (
  repository_source_id uuid NOT NULL
    REFERENCES repository_evidence_sources(id) ON DELETE CASCADE,
  profile_asset_id uuid NOT NULL
    REFERENCES profile_assets(id) ON DELETE CASCADE,
  compiler_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repository_source_id, profile_asset_id)
);

ALTER TABLE profile_asset_evidence_items
  ADD COLUMN IF NOT EXISTS repository_evidence_item_id uuid
    REFERENCES repository_evidence_items(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_profile_asset_evidence_repository_item
  ON profile_asset_evidence_items(repository_evidence_item_id);

CREATE OR REPLACE VIEW v_repository_evidence_review AS
SELECT
  rs.id AS repository_source_id,
  rs.provider,
  rs.repo_full_name,
  rs.canonical_url,
  rs.primary_language,
  rs.topics,
  rs.description,
  rs.is_private,
  rs.is_fork,
  rs.archived,
  rs.ownership_status,
  rs.status,
  count(rei.id) FILTER (WHERE rei.status = 'active') AS active_evidence_count,
  count(rei.id) FILTER (WHERE rei.evidence_type = 'audit_check' AND rei.status = 'active') AS audit_check_count,
  array_agg(rei.evidence_text ORDER BY rei.evidence_type, rei.evidence_key)
    FILTER (WHERE rei.status = 'active') AS evidence_preview,
  array_agg(left(pa.id::text, 8) ORDER BY pa.asset_title)
    FILTER (WHERE pa.id IS NOT NULL) AS linked_asset_short_ids,
  rs.last_seen_at,
  rs.ownership_confirmed_at
FROM repository_evidence_sources rs
LEFT JOIN repository_evidence_items rei
  ON rei.repository_source_id = rs.id
LEFT JOIN repository_evidence_asset_links realink
  ON realink.repository_source_id = rs.id
LEFT JOIN profile_assets pa
  ON pa.id = realink.profile_asset_id
GROUP BY rs.id;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('repository_evidence_ingestor', 'service', 'L2',
   'Imports GitHub metadata and isolated repository-audit output as reviewable evidence without converting code into profile chunks.',
   false, 'active',
   'Ownership is explicit user confirmation. Only approved project assets created from this evidence can be used by L6.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose,
    status = EXCLUDED.status,
    notes = EXCLUDED.notes,
    updated_at = now();

COMMIT;
