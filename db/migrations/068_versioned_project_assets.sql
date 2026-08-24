-- =========================================================
-- 068 -- Versioned project assets and source-authority metadata
-- =========================================================
BEGIN;

ALTER TABLE profile_assets
  ADD COLUMN IF NOT EXISTS project_id text,
  ADD COLUMN IF NOT EXISTS source_snapshot_hash text,
  ADD COLUMN IF NOT EXISTS source_material_hash text,
  ADD COLUMN IF NOT EXISTS freshness_status text NOT NULL DEFAULT 'fresh'
    CHECK (freshness_status IN ('fresh','affected','stale','contradicted','superseded','not_applicable')),
  ADD COLUMN IF NOT EXISTS valid_from timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS valid_until timestamptz,
  ADD COLUMN IF NOT EXISTS source_authority_json jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE repository_evidence_asset_links
  ADD COLUMN IF NOT EXISTS repository_snapshot_id uuid
    REFERENCES repository_snapshots(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_profile_assets_project_freshness
  ON profile_assets(project_id, status, freshness_status);
CREATE INDEX IF NOT EXISTS idx_profile_assets_project_material
  ON profile_assets(project_id, source_material_hash) WHERE source_material_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_repository_evidence_asset_snapshot
  ON repository_evidence_asset_links(repository_source_id, repository_snapshot_id);

-- L6 can never see an approved-but-stale asset.  For a mapped project, a
-- fresh authority-reconciled asset suppresses older raw project assets so
-- stale DOCX implementation wording cannot compete with current GitHub code.
CREATE OR REPLACE VIEW v_document_generation_source_assets AS
SELECT
  pa.id                        AS profile_asset_id,
  pa.asset_title,
  pa.asset_type,
  pa.role_families,
  pa.competency_tags,
  pa.tool_tags,
  pa.job_oriented_summary,
  pa.resume_bullet_bank,
  pa.cover_letter_positioning,
  pa.interview_story,
  pa.do_not_overclaim_rules,
  pa.confidence
FROM profile_assets pa
WHERE pa.status = 'approved'
  AND pa.freshness_status IN ('fresh','not_applicable')
  AND (
    pa.asset_type <> 'project_asset'
    OR pa.project_id IS NULL
    OR pa.source_strategy = 'project_authority_reconciled_v1'
  );

COMMENT ON VIEW v_document_generation_source_assets IS
  'L6 generators read only approved, fresh assets. Once a project is mapped to project_id, only the authority-reconciled project asset is eligible; raw project assets cannot bypass a pending/rejected GitHub-backed revision.';

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('project_source_authority_reconciler', 'service', 'profile',
   'Compile current GitHub implementation evidence with lower-authority project documents into versioned reviewable project assets.',
   false, 'active',
   'Default implementation authority is GitHub 0.70/document 0.30; authority is claim-kind-specific rather than a probability.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
