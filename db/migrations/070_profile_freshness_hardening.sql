-- =========================================================
-- 070 -- Profile freshness hardening
--
-- Keep document-only fixed projects resume-eligible after deterministic project
-- mapping while continuing to suppress raw document assets for GitHub-primary
-- projects until an authority-reconciled asset is approved.
-- =========================================================
BEGIN;

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
    OR pa.source_strategy IN ('project_authority_reconciled_v1','project_document_only_v1')
  );

COMMENT ON VIEW v_document_generation_source_assets IS
  'L6 generators read only approved, fresh assets. GitHub-primary mapped projects require an authority-reconciled asset; explicitly document-only mapped projects use project_document_only_v1.';

INSERT INTO component_registry
  (name, component_type, layer, purpose, trainable, status, notes, created_at, updated_at)
VALUES
  ('profile_freshness_hardening', 'safety', 'L6',
   'Harden project freshness scoping, document-only eligibility, immutable snapshot cache reuse, and bounded last-known-good handling.',
   false, 'active',
   'GitHub implementation controls require code/config evidence; README prose cannot prove implementation.', now(), now())
ON CONFLICT (name) DO UPDATE
SET purpose=EXCLUDED.purpose, status=EXCLUDED.status, notes=EXCLUDED.notes, updated_at=now();

COMMIT;
