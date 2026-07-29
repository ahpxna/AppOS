CREATE OR REPLACE VIEW v_safe_candidate_fact_promotion_queue AS
SELECT
  q.id,
  left(q.id::text, 8) AS short_id,
  q.category,
  q.subcategory,
  q.confidence,
  q.source_file,
  q.file_role,
  q.source_chunk_index,
  q.fact_text,
  q.evidence_quote,
  q.created_at
FROM v_candidate_fact_quality_review q
WHERE q.status = 'pending'
  AND q.quality_bucket IN ('human_review_high_priority', 'human_review_normal')
  AND q.allow_profile_fact_promotion = true
  AND q.evidence_quote IS NOT NULL
  AND length(q.evidence_quote) >= 40

  -- avoid common overclaims / bad phrasing
  AND q.fact_text NOT ILIKE 'User is a member of%'
  AND q.fact_text NOT ILIKE 'User is a career professional%'
  AND q.fact_text NOT ILIKE 'User is a foundation%'
  AND q.fact_text NOT ILIKE 'User is a project-ready%'
  AND q.fact_text NOT ILIKE 'User is a proposed%'
  AND q.fact_text NOT ILIKE 'questions will be examined:%'
  AND q.fact_text NOT ILIKE 'Project CYB-240 =%'
  AND q.fact_text NOT ILIKE '%Nextron Threat Research Team%'
  AND q.fact_text NOT ILIKE '%Splunk, QRadar%'
  AND q.fact_text NOT ILIKE '%CrowdStrike%'
  AND q.fact_text NOT ILIKE '%career professional%'

  -- keep safer fact shapes first
  AND (
    q.fact_text ILIKE 'User has coursework in %'
    OR q.fact_text ILIKE 'User studied %'
    OR q.fact_text ILIKE 'User has developed %'
    OR q.fact_text ILIKE 'User has completed %'
    OR q.fact_text ILIKE 'User has implemented %'
    OR q.fact_text ILIKE 'User conducted %'
    OR q.fact_text ILIKE 'User has conducted %'
    OR q.fact_text ILIKE 'User built %'
    OR q.fact_text ILIKE 'User has built %'
    OR q.fact_text ILIKE 'User measured %'
    OR q.fact_text ILIKE 'User has analyzed %'
    OR q.fact_text ILIKE 'User has worked on %'
  )
ORDER BY
  CASE
    WHEN q.category = 'projects' THEN 1
    WHEN q.category = 'academic' THEN 2
    WHEN q.category = 'research' THEN 3
    WHEN q.category = 'skills' THEN 4
    ELSE 5
  END,
  q.confidence DESC NULLS LAST,
  q.created_at DESC;
