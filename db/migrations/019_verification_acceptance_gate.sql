-- 019_verification_acceptance_gate.sql
-- Hard acceptance gate for profile fact verification suggestions.
-- No verifier output can enter profile_facts unless it passes this gate.

CREATE OR REPLACE VIEW v_candidate_fact_verification_acceptance_gate AS
SELECT
  s.id AS suggestion_id,
  left(s.id::text, 8) AS suggestion_short_id,

  s.candidate_fact_id,
  left(s.candidate_fact_id::text, 8) AS candidate_short_id,

  s.status AS suggestion_status,
  s.verifier_version,
  s.decision,
  s.confidence,

  rf.file_name AS source_file,
  rf.file_role,
  rf.allow_profile_fact_promotion,
  rf.original_local_path,
  rf.parsed_text_path,
  pc.chunk_index AS source_chunk_index,
  pc.section AS source_section,

  s.original_category,
  s.original_subcategory,
  s.original_fact_text,
  s.original_evidence_quote,

  s.suggested_category,
  s.suggested_subcategory,
  s.suggested_fact_text,
  s.suggested_evidence_quote,

  s.evidence_assessment,
  s.context_assessment,
  s.reasoning,
  s.risk_flags,

  CASE
    WHEN s.decision IN ('approve_as_is', 'rewrite')
      AND (
        s.suggested_evidence_quote IS NULL
        OR btrim(s.suggested_evidence_quote) = ''
        OR length(s.suggested_evidence_quote) < 30
      )
    THEN true ELSE false
  END AS flag_missing_or_weak_suggested_evidence,

  CASE
    WHEN (
      s.original_fact_text ILIKE '%member of the Nextron%'
      OR s.suggested_fact_text ILIKE '%member of the Nextron%'
      OR s.original_fact_text ILIKE '%Threat Research Team%'
      OR s.suggested_fact_text ILIKE '%Threat Research Team%'
    )
    THEN true ELSE false
  END AS flag_external_membership_claim,

  CASE
    WHEN (
      s.original_fact_text ILIKE '%technical skills in CMP%'
      OR s.suggested_fact_text ILIKE '%technical skills in CMP%'
      OR s.original_fact_text ILIKE '%technical skills in COM%'
      OR s.suggested_fact_text ILIKE '%technical skills in COM%'
      OR s.original_fact_text ILIKE '%technical skills in MCS%'
      OR s.suggested_fact_text ILIKE '%technical skills in MCS%'
    )
    THEN true ELSE false
  END AS flag_transcript_course_list_as_skill,

  CASE
    WHEN rf.file_name ILIKE '%Transcript%'
      AND (
        s.original_fact_text ILIKE '%technical skills%'
        OR s.suggested_fact_text ILIKE '%technical skills%'
        OR s.original_fact_text ILIKE '%proficient%'
        OR s.suggested_fact_text ILIKE '%proficient%'
        OR s.original_fact_text ILIKE '%experience with%'
        OR s.suggested_fact_text ILIKE '%experience with%'
      )
    THEN true ELSE false
  END AS flag_transcript_overclaim,

  CASE
    WHEN (
      s.original_fact_text ILIKE 'questions will be examined:%'
      OR s.suggested_fact_text ILIKE 'questions will be examined:%'
      OR s.original_fact_text ILIKE 'Project CYB-240 =%'
      OR s.suggested_fact_text ILIKE 'Project CYB-240 =%'
      OR s.original_fact_text ILIKE '%accepted for publication%'
      OR s.suggested_fact_text ILIKE '%accepted for publication%'
    )
    THEN true ELSE false
  END AS flag_title_or_fragment_as_fact,

  CASE
    WHEN (
      s.original_evidence_quote ILIKE '%should%'
      OR s.suggested_evidence_quote ILIKE '%should%'
      OR s.original_evidence_quote ILIKE '%recommend%'
      OR s.suggested_evidence_quote ILIKE '%recommend%'
      OR s.original_evidence_quote ILIKE '%nên%'
      OR s.suggested_evidence_quote ILIKE '%nên%'
      OR s.original_evidence_quote ILIKE '%bổ sung%'
      OR s.suggested_evidence_quote ILIKE '%bổ sung%'
    )
    THEN true ELSE false
  END AS flag_guidance_language,

  CASE
    WHEN s.decision = 'approve_as_is'
      AND btrim(COALESCE(s.suggested_fact_text, '')) <> btrim(COALESCE(s.original_fact_text, ''))
    THEN true ELSE false
  END AS flag_approve_as_is_but_changed_text,

  CASE
    WHEN rf.allow_profile_fact_promotion IS NOT TRUE
    THEN true ELSE false
  END AS flag_source_not_promotable,

  CASE
    WHEN rf.path_status IS DISTINCT FROM 'verified'
    THEN true ELSE false
  END AS flag_source_path_not_verified,

  CASE
    WHEN s.status <> 'pending'
    THEN 'not_pending'
    WHEN s.decision NOT IN ('approve_as_is', 'rewrite')
    THEN 'not_accept_candidate'
    WHEN rf.allow_profile_fact_promotion IS NOT TRUE
    THEN 'blocked_source_not_promotable'
    WHEN rf.path_status IS DISTINCT FROM 'verified'
    THEN 'blocked_source_path_not_verified'
    WHEN s.decision IN ('approve_as_is', 'rewrite')
      AND (
        s.suggested_evidence_quote IS NULL
        OR btrim(s.suggested_evidence_quote) = ''
        OR length(s.suggested_evidence_quote) < 30
      )
    THEN 'blocked_weak_evidence'
    WHEN (
      s.original_fact_text ILIKE '%member of the Nextron%'
      OR s.suggested_fact_text ILIKE '%member of the Nextron%'
      OR s.original_fact_text ILIKE '%Threat Research Team%'
      OR s.suggested_fact_text ILIKE '%Threat Research Team%'
    )
    THEN 'blocked_external_membership_claim'
    WHEN (
      s.original_fact_text ILIKE '%technical skills in CMP%'
      OR s.suggested_fact_text ILIKE '%technical skills in CMP%'
      OR s.original_fact_text ILIKE '%technical skills in COM%'
      OR s.suggested_fact_text ILIKE '%technical skills in COM%'
      OR s.original_fact_text ILIKE '%technical skills in MCS%'
      OR s.suggested_fact_text ILIKE '%technical skills in MCS%'
    )
    THEN 'blocked_transcript_course_list_as_skill'
    WHEN rf.file_name ILIKE '%Transcript%'
      AND (
        s.original_fact_text ILIKE '%technical skills%'
        OR s.suggested_fact_text ILIKE '%technical skills%'
        OR s.original_fact_text ILIKE '%proficient%'
        OR s.suggested_fact_text ILIKE '%proficient%'
        OR s.original_fact_text ILIKE '%experience with%'
        OR s.suggested_fact_text ILIKE '%experience with%'
      )
    THEN 'blocked_transcript_overclaim'
    WHEN (
      s.original_fact_text ILIKE 'questions will be examined:%'
      OR s.suggested_fact_text ILIKE 'questions will be examined:%'
      OR s.original_fact_text ILIKE 'Project CYB-240 =%'
      OR s.suggested_fact_text ILIKE 'Project CYB-240 =%'
      OR s.original_fact_text ILIKE '%accepted for publication%'
      OR s.suggested_fact_text ILIKE '%accepted for publication%'
    )
    THEN 'blocked_title_or_fragment_as_fact'
    WHEN (
      s.original_evidence_quote ILIKE '%should%'
      OR s.suggested_evidence_quote ILIKE '%should%'
      OR s.original_evidence_quote ILIKE '%recommend%'
      OR s.suggested_evidence_quote ILIKE '%recommend%'
      OR s.original_evidence_quote ILIKE '%nên%'
      OR s.suggested_evidence_quote ILIKE '%nên%'
      OR s.original_evidence_quote ILIKE '%bổ sung%'
      OR s.suggested_evidence_quote ILIKE '%bổ sung%'
    )
    THEN 'needs_human_or_remote_audit_guidance_language'
    WHEN s.decision = 'approve_as_is'
      AND btrim(COALESCE(s.suggested_fact_text, '')) <> btrim(COALESCE(s.original_fact_text, ''))
    THEN 'needs_human_or_remote_audit_changed_text'
    WHEN s.confidence < 0.90
    THEN 'needs_human_or_remote_audit_low_confidence'
    ELSE 'eligible_for_human_accept'
  END AS acceptance_status

FROM candidate_fact_verification_suggestions s
JOIN candidate_profile_facts cpf
  ON cpf.id = s.candidate_fact_id
LEFT JOIN raw_files rf
  ON rf.id = s.source_file_id
LEFT JOIN profile_chunks pc
  ON pc.id = s.source_chunk_id;

CREATE OR REPLACE VIEW v_candidate_fact_verification_acceptance_queue AS
SELECT *
FROM v_candidate_fact_verification_acceptance_gate
WHERE acceptance_status = 'eligible_for_human_accept'
ORDER BY confidence DESC NULLS LAST, suggestion_short_id;

CREATE OR REPLACE VIEW v_candidate_fact_verification_blocked AS
SELECT *
FROM v_candidate_fact_verification_acceptance_gate
WHERE acceptance_status LIKE 'blocked_%'
   OR acceptance_status LIKE 'needs_human_or_remote_audit_%'
ORDER BY acceptance_status, confidence DESC NULLS LAST;
