-- 012_file_roles_and_candidate_triage.sql
-- Classify source files so profile facts do not confuse personal evidence with career guidance/reference material.

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS file_role text DEFAULT 'unclassified';

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS evidence_weight numeric DEFAULT 0.50;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS allow_profile_fact_promotion boolean DEFAULT false;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS allow_profile_pack_retrieval boolean DEFAULT true;

ALTER TABLE raw_files
ADD COLUMN IF NOT EXISTS file_role_notes text;

-- Primary personal evidence.
UPDATE raw_files
SET
  file_role = 'primary_profile_evidence',
  evidence_weight = 0.95,
  allow_profile_fact_promotion = true,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'Primary user-owned evidence such as transcript or master resume.'
WHERE
  file_name ILIKE '%Transcript%'
  OR file_name ILIKE '%Master Baseline Resume%';

-- Enriched profile/course/project writeups created to describe user's experience.
UPDATE raw_files
SET
  file_role = 'enriched_profile_evidence',
  evidence_weight = 0.85,
  allow_profile_fact_promotion = true,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'User profile enrichment document; usable for candidate facts with human review.'
WHERE
  (
    file_name ILIKE '%Data Enrichment Profile%'
    OR file_name ILIKE '%Course Profile%'
    OR file_name ILIKE '%Project Profile%'
    OR file_name ILIKE '%Short Data Enrichment Profile%'
    OR file_name ILIKE '%Complete Data Enrichment Profile%'
    OR file_name ILIKE '%Revised Full Profile%'
    OR file_name ILIKE '%Source Mapping%'
    OR file_name ILIKE '%Tools.docx%'
    OR file_name ILIKE '%TỔNG HỢP%'
    OR file_name ILIKE '%TỔNG HỢP%'
  )
  AND file_role = 'unclassified';

-- Research/project artifacts. Usable, but with caution: do not claim publication/completion unless explicit.
UPDATE raw_files
SET
  file_role = 'project_artifact_evidence',
  evidence_weight = 0.80,
  allow_profile_fact_promotion = true,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'Project or research artifact; promote only evidence-grounded claims.'
WHERE
  (
    file_name ILIKE '%Causal_Influence_Graph%'
    OR file_name ILIKE '%CIG%'
    OR file_name ILIKE '%Project_Paper%'
    OR file_name ILIKE '%Implementation_Details%'
  )
  AND file_role = 'unclassified';

-- Career strategy / market guidance. Useful for strategy, not direct personal fact evidence.
UPDATE raw_files
SET
  file_role = 'career_strategy_guidance',
  evidence_weight = 0.35,
  allow_profile_fact_promotion = false,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'Career/qualification guidance. Do not promote direct user profile facts from this without separate evidence.'
WHERE
  (
    file_name ILIKE '%Báo cáo nghiên cứu sâu%'
    OR file_name ILIKE '%qualifications%'
    OR file_name ILIKE '%Calculus Qualifications Portfolio Mapping%'
  )
  AND file_role = 'unclassified';

-- Course/reference PDFs. Useful context, not direct personal evidence unless linked to transcript/project.
UPDATE raw_files
SET
  file_role = 'course_reference_material',
  evidence_weight = 0.45,
  allow_profile_fact_promotion = false,
  allow_profile_pack_retrieval = true,
  file_role_notes = 'Course/reference material. Use as background; avoid direct personal claims unless supported elsewhere.'
WHERE
  (
    file_name ~ '^[A-Z]{2,4}_?[0-9]{3}'
    OR file_name ILIKE 'CSC_%'
    OR file_name ILIKE 'CYB_%'
  )
  AND file_role = 'unclassified';

CREATE OR REPLACE VIEW v_raw_file_roles AS
SELECT
  id,
  left(id::text, 8) AS short_id,
  file_name,
  source,
  parse_status,
  file_role,
  evidence_weight,
  allow_profile_fact_promotion,
  allow_profile_pack_retrieval,
  file_role_notes,
  uploaded_at
FROM raw_files
ORDER BY file_role, file_name, uploaded_at DESC;

CREATE OR REPLACE VIEW v_candidate_fact_triage AS
SELECT
  cpf.id,
  left(cpf.id::text, 8) AS short_id,
  cpf.status,
  cpf.category,
  cpf.subcategory,
  cpf.confidence,
  rf.file_name AS source_file,
  rf.file_role,
  rf.evidence_weight,
  rf.allow_profile_fact_promotion,
  pc.chunk_index AS source_chunk_index,
  pc.section AS source_section,
  cpf.fact_text,
  cpf.evidence_quote,
  cpf.reasoning,
  cpf.created_at,

  CASE
    WHEN cpf.evidence_quote IS NULL OR btrim(cpf.evidence_quote) = '' THEN 'reject_missing_evidence'
    WHEN rf.allow_profile_fact_promotion = false THEN 'do_not_promote_source_role'
    WHEN lower(cpf.evidence_quote) LIKE '%in progress%' THEN 'review_in_progress'
    WHEN lower(cpf.evidence_quote) LIKE '%confirm%' THEN 'review_needs_confirmation'
    WHEN cpf.confidence >= 0.90 AND rf.evidence_weight >= 0.80 THEN 'high_priority_review'
    WHEN cpf.confidence >= 0.75 AND rf.evidence_weight >= 0.70 THEN 'normal_review'
    ELSE 'low_priority_review'
  END AS triage_bucket

FROM candidate_profile_facts cpf
LEFT JOIN raw_files rf
  ON rf.id = cpf.source_file_id
LEFT JOIN profile_chunks pc
  ON pc.id = cpf.source_chunk_id
WHERE cpf.extractor_name = 'local_ollama_candidate_fact_extractor'
  AND cpf.extractor_version = 'ollama_structured_v2_require_evidence_2026_04_26'
ORDER BY
  CASE
    WHEN cpf.status = 'pending' THEN 1
    WHEN cpf.status = 'needs_edit' THEN 2
    WHEN cpf.status = 'approved' THEN 3
    WHEN cpf.status = 'promoted' THEN 4
    WHEN cpf.status = 'rejected' THEN 5
    ELSE 6
  END,
  CASE
    WHEN rf.allow_profile_fact_promotion = true THEN 1 ELSE 2
  END,
  cpf.confidence DESC NULLS LAST,
  cpf.created_at DESC;
