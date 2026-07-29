-- 008_candidate_profile_facts.sql
-- Bảng chứa facts do LLM đề xuất.
-- Facts ở đây chưa phải sự thật chính thức cho tới khi user duyệt và promote sang profile_facts.

CREATE TABLE IF NOT EXISTS candidate_profile_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  source_file_id uuid REFERENCES raw_files(id) ON DELETE CASCADE,
  source_chunk_id uuid REFERENCES profile_chunks(id) ON DELETE CASCADE,

  extractor_name text NOT NULL,
  extractor_version text NOT NULL,

  category text,
  subcategory text,

  fact_text text NOT NULL,
  evidence_quote text,
  reasoning text,

  confidence numeric,

  status text NOT NULL DEFAULT 'pending',
  -- pending    = đang chờ review
  -- approved   = user đã approve candidate này
  -- rejected   = user reject
  -- needs_edit = cần sửa trước khi approve
  -- promoted   = đã được copy sang profile_facts

  review_note text,

  promoted_profile_fact_id uuid REFERENCES profile_facts(id) ON DELETE SET NULL,

  dedup_key text,
  conflict_group_id uuid,

  created_at timestamptz DEFAULT now(),
  reviewed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_candidate_profile_facts_status
ON candidate_profile_facts(status);

CREATE INDEX IF NOT EXISTS idx_candidate_profile_facts_category
ON candidate_profile_facts(category, subcategory);

CREATE INDEX IF NOT EXISTS idx_candidate_profile_facts_source_file
ON candidate_profile_facts(source_file_id);

CREATE INDEX IF NOT EXISTS idx_candidate_profile_facts_source_chunk
ON candidate_profile_facts(source_chunk_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_profile_facts_dedup_key
ON candidate_profile_facts(dedup_key)
WHERE dedup_key IS NOT NULL;

CREATE OR REPLACE VIEW v_candidate_profile_facts AS
SELECT
  cpf.id,
  cpf.status,
  cpf.category,
  cpf.subcategory,
  cpf.confidence,
  cpf.fact_text,
  cpf.evidence_quote,
  cpf.reasoning,
  rf.file_name AS source_file,
  pc.chunk_index AS source_chunk_index,
  pc.section AS source_section,
  pc.category AS source_chunk_category,
  cpf.extractor_name,
  cpf.extractor_version,
  cpf.created_at,
  cpf.reviewed_at,
  cpf.review_note,
  cpf.promoted_profile_fact_id
FROM candidate_profile_facts cpf
LEFT JOIN raw_files rf
  ON rf.id = cpf.source_file_id
LEFT JOIN profile_chunks pc
  ON pc.id = cpf.source_chunk_id
ORDER BY
  CASE cpf.status
    WHEN 'pending' THEN 1
    WHEN 'needs_edit' THEN 2
    WHEN 'approved' THEN 3
    WHEN 'promoted' THEN 4
    WHEN 'rejected' THEN 5
    ELSE 6
  END,
  cpf.confidence DESC NULLS LAST,
  cpf.created_at DESC;
