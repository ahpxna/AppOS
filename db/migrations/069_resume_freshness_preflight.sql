-- =========================================================
-- 069 -- Resume freshness aggregate and generation gate
-- =========================================================
BEGIN;

CREATE OR REPLACE VIEW v_project_freshness_summary AS
SELECT
  rs.id AS repository_source_id,
  rs.repo_full_name,
  rs.freshness_status AS repository_freshness_status,
  rs.current_snapshot_id,
  rs.last_analyzed_snapshot_id,
  count(rc.id) FILTER (WHERE rc.freshness_status IN ('affected','contradicted','source_missing')) AS blocking_claims,
  count(rc.id) FILTER (WHERE rc.freshness_status IN ('fresh','revalidated')) AS usable_claims,
  max(rc.last_seen_at) AS last_claim_seen_at
FROM repository_evidence_sources rs
LEFT JOIN repository_claims rc ON rc.repository_source_id = rs.id
GROUP BY rs.id;

CREATE OR REPLACE VIEW v_resume_profile_freshness_gate AS
SELECT
  (SELECT count(*) FROM candidate_fixed_fields
    WHERE show_on_resume = true
      AND verification_status NOT IN ('user_verified','document_verified','excluded')) AS unresolved_fixed_fields,
  (SELECT count(*) FROM candidate_certifications
    WHERE show_on_resume = true
      AND (certification_status <> 'earned'
           OR verification_status NOT IN ('user_verified','document_verified')
           OR (expires_at IS NOT NULL AND expires_at < current_date))) AS invalid_visible_certifications,
  (SELECT count(*) FROM repository_evidence_sources
    WHERE ownership_status = 'confirmed_by_user'
      AND freshness_status NOT IN ('fresh','excluded')) AS stale_repository_sources,
  (SELECT count(*) FROM repository_claims
    WHERE freshness_status IN ('affected','contradicted','source_missing')) AS stale_repository_claims,
  (SELECT count(*) FROM project_source_conflicts WHERE status = 'open') AS open_project_conflicts,
  (SELECT count(*) FROM profile_assets
    WHERE status = 'approved' AND asset_type = 'project_asset'
      AND freshness_status NOT IN ('fresh','not_applicable')) AS stale_approved_project_assets;

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('resume_freshness_preflight', 'safety', 'L6',
   'Block resume generation when fixed fields, certifications, configured GitHub projects, project claims or project assets are stale or unresolved.',
   false, 'active',
   'A live GitHub refresh runs before resume generation unless explicitly disabled for offline diagnostics.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose = EXCLUDED.purpose, status = EXCLUDED.status, notes = EXCLUDED.notes, updated_at = now();

COMMIT;
