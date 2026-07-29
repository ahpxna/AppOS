CREATE OR REPLACE VIEW v_clean_promotable_candidate_facts AS
SELECT *
FROM v_candidate_fact_quality_review
WHERE status = 'pending'
  AND quality_bucket = 'human_review_high_priority'
  AND fact_text NOT ILIKE 'User has skills in %'
  AND fact_text NOT ILIKE 'User has a strong%'
  AND fact_text NOT ILIKE 'User knows%'
  AND fact_text NOT ILIKE 'User is interested%'
  AND evidence_quote IS NOT NULL
  AND length(evidence_quote) > 30;
