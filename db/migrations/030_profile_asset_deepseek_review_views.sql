-- =========================================================
-- 030 — Profile Asset DeepSeek Review Views
-- Purpose:
--   Create a safe review layer after DeepSeek structured asset audit.
--   This does not approve assets automatically.
--   It only surfaces audited assets and approval candidates.
-- =========================================================

CREATE OR REPLACE VIEW v_profile_asset_deepseek_review AS
WITH evidence_counts AS (
  SELECT
    profile_asset_id,
    count(*) AS evidence_item_count
  FROM profile_asset_evidence_items
  GROUP BY profile_asset_id
)
SELECT
  pa.id AS profile_asset_id,
  left(pa.id::text, 8) AS asset_short_id,

  pa.asset_title,
  pa.asset_type,
  pa.abstraction_level,
  pa.status AS asset_status,
  pa.confidence,

  pa.role_families,
  pa.competency_tags,
  pa.tool_tags,
  pa.project_tags,

  coalesce(ec.evidence_item_count, 0) AS evidence_item_count,

  paa.grounding_status,
  paa.overclaim_risk,
  paa.information_loss_risk,
  paa.evidence_coverage_score,
  paa.specificity_score,
  paa.job_relevance_score,
  paa.supported_claims,
  paa.unsupported_claims,
  paa.required_edits,
  paa.audit_notes,

  left(pa.canonical_narrative, 900) AS canonical_narrative_preview,
  left(pa.job_oriented_summary, 700) AS job_oriented_summary_preview,
  left(pa.resume_bullet_bank, 700) AS resume_bullet_bank_preview,
  left(pa.interview_story, 700) AS interview_story_preview,
  pa.do_not_overclaim_rules,

  pa.compiler_version,
  pa.created_at AS asset_created_at,
  pa.updated_at AS asset_updated_at,
  paa.created_at AS audited_at,

  CASE
    WHEN paa.grounding_status = 'grounded'
      AND paa.overclaim_risk = 'low'
      AND paa.information_loss_risk = 'low'
      AND coalesce(array_length(paa.required_edits, 1), 0) = 0
      AND coalesce(ec.evidence_item_count, 0) >= 2
    THEN 'ready_for_user_approval'

    WHEN paa.grounding_status = 'grounded'
      AND paa.overclaim_risk IN ('low', 'medium')
      AND paa.information_loss_risk IN ('low', 'medium')
    THEN 'review_before_approval'

    WHEN paa.grounding_status IN ('blocked', 'ungrounded')
      OR paa.overclaim_risk = 'high'
      OR paa.information_loss_risk = 'high'
    THEN 'block_or_rewrite'

    ELSE 'manual_review'
  END AS review_recommendation

FROM profile_assets pa
JOIN profile_asset_audits paa
  ON paa.profile_asset_id = pa.id
LEFT JOIN evidence_counts ec
  ON ec.profile_asset_id = pa.id
WHERE paa.audit_type = 'deepseek_structured_asset_grounding_overclaim_audit'
  AND paa.audit_version = 'deepseek_structured_asset_audit_v1_2026_04_27';

CREATE OR REPLACE VIEW v_profile_asset_approval_candidates AS
SELECT *
FROM v_profile_asset_deepseek_review
WHERE review_recommendation = 'ready_for_user_approval'
  AND asset_status IN ('draft', 'needs_review', 'pending_review');

CREATE OR REPLACE VIEW v_profile_asset_deepseek_audit_summary AS
SELECT
  asset_status,
  grounding_status,
  overclaim_risk,
  information_loss_risk,
  review_recommendation,
  count(*) AS asset_count
FROM v_profile_asset_deepseek_review
GROUP BY
  asset_status,
  grounding_status,
  overclaim_risk,
  information_loss_risk,
  review_recommendation
ORDER BY
  asset_status,
  grounding_status,
  overclaim_risk,
  information_loss_risk,
  review_recommendation;
