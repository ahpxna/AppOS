-- 024_profile_retrieval_signals.sql
-- Evidence-aware chunk retrieval signals for Profile Retrieval API.
-- This makes retrieval distinguish evidence chunks from guidance/roadmap/header chunks.

ALTER TABLE profile_retrieval_results
ADD COLUMN IF NOT EXISTS retrieval_bucket text;

ALTER TABLE profile_retrieval_results
ADD COLUMN IF NOT EXISTS retrieval_signal_score numeric;

ALTER TABLE profile_retrieval_results
ADD COLUMN IF NOT EXISTS rerank_score numeric;

ALTER TABLE profile_retrieval_results
ADD COLUMN IF NOT EXISTS negative_retrieval_flags jsonb DEFAULT '{}'::jsonb;

CREATE OR REPLACE VIEW v_profile_chunk_retrieval_signals AS
WITH base AS (
  SELECT
    pc.id AS chunk_id,
    rf.id AS file_id,
    rf.file_name,
    rf.file_role,
    rf.evidence_weight,
    rf.allow_profile_fact_promotion,
    pc.chunk_index,
    pc.section,
    pc.category,
    pc.text_content,
    lower(concat_ws(' ', rf.file_name, pc.section, pc.category, pc.text_content)) AS text_blob
  FROM profile_chunks pc
  JOIN raw_files rf
    ON rf.id = pc.file_id
  WHERE rf.source = 'local_profile_ingestion'
    AND rf.is_active = true
    AND rf.path_status = 'verified'
    AND pc.text_content IS NOT NULL
    AND length(btrim(pc.text_content)) > 0
),
flags AS (
  SELECT
    *,

    (
      text_blob LIKE '%new jersey, united states%'
      OR text_blob LIKE '%linkedin: [add link]%'
      OR text_blob LIKE '%github: [add link]%'
      OR text_blob LIKE '%portfolio: [add link]%'
      OR (
        text_blob LIKE '%master baseline resume%'
        AND chunk_index <= 1
      )
    ) AS flag_header_or_contact,

    (
      text_blob LIKE '%roadmap%'
      OR text_blob LIKE '%future targeting%'
      OR text_blob LIKE '%not current credentials%'
      OR text_blob LIKE '%prioritize next%'
      OR text_blob LIKE '%highest priority:%'
      OR text_blob LIKE '%certification%'
    ) AS flag_roadmap_or_future,

    (
      text_blob LIKE '%should be positioned%'
      OR text_blob LIKE '%should be moved%'
      OR text_blob LIKE '%should prioritize%'
      OR text_blob LIKE '%good wording:%'
      OR text_blob LIKE '%how to present%'
      OR text_blob LIKE '%resume phrase:%'
      OR text_blob LIKE '%useful for%'
      OR text_blob LIKE '%relevant roles:%'
      OR text_blob LIKE '%career relevance%'
      OR text_blob LIKE '%nên%'
      OR text_blob LIKE '%có thể dùng%'
      OR text_blob LIKE '%có thể mô tả%'
    ) AS flag_guidance_language,

    (
      text_blob LIKE '%final positioning%'
      OR text_blob LIKE '%core positioning%'
      OR text_blob LIKE '%positioning statement%'
      OR text_blob LIKE '%portfolio positioning%'
      OR text_blob LIKE '%resume-ready summary%'
      OR text_blob LIKE '%project-ready positioning%'
    ) AS flag_positioning_summary,

    (
      text_blob LIKE '%implemented%'
      OR text_blob LIKE '%built%'
      OR text_blob LIKE '%developed%'
      OR text_blob LIKE '%completed%'
      OR text_blob LIKE '%conducted%'
      OR text_blob LIKE '%analyzed%'
      OR text_blob LIKE '%measured%'
      OR text_blob LIKE '%evaluated%'
      OR text_blob LIKE '%configured%'
      OR text_blob LIKE '%tested%'
      OR text_blob LIKE '%simulated%'
      OR text_blob LIKE '%coursework%'
      OR text_blob LIKE '%lab%'
      OR text_blob LIKE '%project%'
      OR text_blob LIKE '%studied%'
      OR text_blob LIKE '%mapped%'
      OR text_blob LIKE '%used%'
    ) AS flag_evidence_action,

    (
      text_blob LIKE '%cyb %'
      OR text_blob LIKE '%csc %'
      OR text_blob LIKE '%cis%'
      OR text_blob LIKE '%linux%'
      OR text_blob LIKE '%gns3%'
      OR text_blob LIKE '%nmap%'
      OR text_blob LIKE '%tcpdump%'
      OR text_blob LIKE '%wireshark%'
      OR text_blob LIKE '%burp%'
      OR text_blob LIKE '%forensics%'
      OR text_blob LIKE '%firewall%'
      OR text_blob LIKE '%radius%'
      OR text_blob LIKE '%syslog%'
      OR text_blob LIKE '%pki%'
      OR text_blob LIKE '%ocsp%'
      OR text_blob LIKE '%lockbit%'
      OR text_blob LIKE '%dirty pipe%'
      OR text_blob LIKE '%sql%'
      OR text_blob LIKE '%java%'
      OR text_blob LIKE '%python%'
    ) AS flag_concrete_terms
  FROM base
)
SELECT
  chunk_id,
  file_id,
  file_name,
  file_role,
  evidence_weight,
  allow_profile_fact_promotion,
  chunk_index,
  section,
  category,
  text_content,

  CASE
    WHEN flag_header_or_contact THEN 'header_or_contact'
    WHEN flag_roadmap_or_future THEN 'roadmap_or_future'
    WHEN flag_guidance_language THEN 'guidance'
    WHEN flag_positioning_summary THEN 'background_summary'
    WHEN flag_evidence_action OR flag_concrete_terms THEN 'evidence'
    ELSE 'low_signal'
  END AS retrieval_bucket,

  (
    COALESCE(evidence_weight, 0.50)
    + CASE file_role
        WHEN 'primary_profile_evidence' THEN 0.12
        WHEN 'project_artifact_evidence' THEN 0.10
        WHEN 'enriched_profile_evidence' THEN 0.04
        WHEN 'course_reference_material' THEN -0.10
        WHEN 'career_strategy_guidance' THEN -0.30
        ELSE -0.10
      END
    + CASE WHEN flag_evidence_action THEN 0.12 ELSE 0 END
    + CASE WHEN flag_concrete_terms THEN 0.08 ELSE 0 END
    + CASE WHEN flag_positioning_summary THEN -0.08 ELSE 0 END
    + CASE WHEN flag_guidance_language THEN -0.35 ELSE 0 END
    + CASE WHEN flag_roadmap_or_future THEN -0.45 ELSE 0 END
    + CASE WHEN flag_header_or_contact THEN -0.55 ELSE 0 END
  ) AS retrieval_signal_score,

  jsonb_strip_nulls(jsonb_build_object(
    'header_or_contact', CASE WHEN flag_header_or_contact THEN true ELSE NULL END,
    'roadmap_or_future', CASE WHEN flag_roadmap_or_future THEN true ELSE NULL END,
    'guidance_language', CASE WHEN flag_guidance_language THEN true ELSE NULL END,
    'positioning_summary', CASE WHEN flag_positioning_summary THEN true ELSE NULL END
  )) AS negative_retrieval_flags
FROM flags;

-- NOTE: CREATE OR REPLACE VIEW can only APPEND new columns at the very end
-- of the existing SELECT list -- it cannot reorder or insert columns among
-- ones that already exist in a previously-created version of this view
-- (023_profile_retrieval_api.sql). The four new signal columns below
-- (rerank_score, retrieval_bucket, retrieval_signal_score,
-- negative_retrieval_flags) must stay after every column that already
-- existed in 023's version, in their original order, or Postgres raises
-- "cannot change name of view column ... to ...". Confirmed live: an
-- earlier version of this file put the new columns before chunk_short_id,
-- which failed on a real install with exactly that error.
CREATE OR REPLACE VIEW v_profile_retrieval_latest_results AS
SELECT
  q.id AS retrieval_query_id,
  left(q.id::text, 8) AS retrieval_short_id,
  q.purpose,
  q.role_family,
  q.query_text,
  q.embedding_model,
  q.max_chunks,
  q.status AS query_status,

  r.rank,
  r.similarity,
  r.distance,
  left(r.chunk_id::text, 8) AS chunk_short_id,
  r.file_name,
  r.file_role,
  r.chunk_index,
  r.section,
  r.category,
  r.text_preview,

  q.created_at,

  -- new in this migration -- must stay appended at the end, see note above
  r.rerank_score,
  r.retrieval_bucket,
  r.retrieval_signal_score,
  r.negative_retrieval_flags
FROM profile_retrieval_queries q
JOIN profile_retrieval_results r
  ON r.retrieval_query_id = q.id
ORDER BY q.created_at DESC, r.rank ASC;
