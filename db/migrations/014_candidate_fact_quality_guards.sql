-- 014_candidate_fact_quality_guards.sql
-- Quality guard views for candidate facts before human review/promote.

CREATE OR REPLACE VIEW v_candidate_fact_quality_flags AS
SELECT
  cft.*,

  CASE
    WHEN evidence_quote IS NULL OR btrim(evidence_quote) = '' THEN true
    ELSE false
  END AS flag_missing_evidence,

  CASE
    WHEN lower(evidence_quote) LIKE '%in progress%'
      OR lower(evidence_quote) LIKE '%currently taking%'
      OR lower(evidence_quote) LIKE '%planned%'
      OR lower(evidence_quote) LIKE '%prioritize next%'
      OR lower(evidence_quote) LIKE '%should%'
      OR lower(evidence_quote) LIKE '%recommend%'
      OR lower(evidence_quote) LIKE '%bổ sung%'
      OR lower(evidence_quote) LIKE '%nên%'
    THEN true
    ELSE false
  END AS flag_future_or_guidance_language,

  CASE
    WHEN fact_text ILIKE '%Computer Networks and Operating Systems and Cybersecurity%'
      AND evidence_quote NOT ILIKE '%Computer Networks%'
      AND evidence_quote NOT ILIKE '%Operating Systems%'
      AND evidence_quote NOT ILIKE '%Cybersecurity%'
    THEN true
    ELSE false
  END AS flag_generic_coursework_mismatch,

  CASE
    WHEN fact_text ILIKE '%Bachelor of Science senior at Rider University%'
      AND evidence_quote NOT ILIKE '%Bachelor of Science%'
      AND evidence_quote NOT ILIKE '%senior at Rider University%'
    THEN true
    ELSE false
  END AS flag_degree_claim_weak_evidence,

  CASE
    WHEN fact_text ILIKE 'User has skills in %'
      AND (
        evidence_quote ILIKE '%career paths%'
        OR evidence_quote ILIKE '%role%'
        OR evidence_quote ILIKE '%supports%'
        OR evidence_quote ILIKE '%có giá trị%'
      )
    THEN true
    ELSE false
  END AS flag_broad_skill_from_career_relevance,

  CASE
    WHEN allow_profile_fact_promotion = false THEN true
    ELSE false
  END AS flag_source_not_promotable

FROM v_candidate_fact_triage cft;

CREATE OR REPLACE VIEW v_candidate_fact_quality_review AS
SELECT
  *,
  CASE
    WHEN flag_missing_evidence THEN 'reject_missing_evidence'
    WHEN flag_source_not_promotable THEN 'reject_source_not_promotable'
    WHEN flag_generic_coursework_mismatch THEN 'reject_generic_coursework_mismatch'
    WHEN flag_degree_claim_weak_evidence THEN 'reject_degree_claim_weak_evidence'
    WHEN flag_future_or_guidance_language THEN 'needs_edit_future_or_guidance'
    WHEN flag_broad_skill_from_career_relevance THEN 'needs_edit_broad_skill_from_career_relevance'
    WHEN triage_bucket = 'high_priority_review' THEN 'human_review_high_priority'
    WHEN triage_bucket = 'normal_review' THEN 'human_review_normal'
    ELSE 'human_review_low'
  END AS quality_bucket
FROM v_candidate_fact_quality_flags
ORDER BY
  CASE
    WHEN status = 'pending' THEN 1
    WHEN status = 'needs_edit' THEN 2
    WHEN status = 'approved' THEN 3
    WHEN status = 'promoted' THEN 4
    WHEN status = 'rejected' THEN 5
    ELSE 6
  END,
  CASE
    WHEN flag_missing_evidence THEN 1
    WHEN flag_source_not_promotable THEN 2
    WHEN flag_generic_coursework_mismatch THEN 3
    WHEN flag_degree_claim_weak_evidence THEN 4
    WHEN flag_future_or_guidance_language THEN 5
    WHEN flag_broad_skill_from_career_relevance THEN 6
    ELSE 7
  END,
  confidence DESC NULLS LAST,
  created_at DESC;
