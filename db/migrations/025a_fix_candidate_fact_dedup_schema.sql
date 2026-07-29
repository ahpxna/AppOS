-- 025a_fix_candidate_fact_dedup_schema.sql
-- Align old semantic dedup tables with production schema.
-- Do not delete old data.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS dedup_version text;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS group_fingerprint text;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS group_type text;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS group_status text DEFAULT 'pending_review';

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS canonical_candidate_fact_id uuid REFERENCES candidate_profile_facts(id) ON DELETE SET NULL;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS member_count integer DEFAULT 0;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS avg_similarity numeric;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS max_similarity numeric;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS group_confidence numeric;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS representative_text text;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS reasoning text;

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

ALTER TABLE candidate_fact_dedup_groups
ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

UPDATE candidate_fact_dedup_groups
SET
  dedup_version = COALESCE(dedup_version, 'legacy_unknown'),
  group_fingerprint = COALESCE(group_fingerprint, id::text),
  group_type = COALESCE(group_type, 'semantic_duplicate'),
  group_status = COALESCE(group_status, 'pending_review'),
  member_count = COALESCE(member_count, 0),
  updated_at = COALESCE(updated_at, now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_fact_dedup_groups_version_fingerprint
ON candidate_fact_dedup_groups(dedup_version, group_fingerprint);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_groups_status
ON candidate_fact_dedup_groups(group_status);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_groups_type
ON candidate_fact_dedup_groups(group_type);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_groups_canonical
ON candidate_fact_dedup_groups(canonical_candidate_fact_id);

CREATE TABLE IF NOT EXISTS candidate_fact_dedup_group_members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id uuid NOT NULL REFERENCES candidate_fact_dedup_groups(id) ON DELETE CASCADE,
  candidate_fact_id uuid NOT NULL REFERENCES candidate_profile_facts(id) ON DELETE CASCADE,
  member_role text,
  suggested_action text,
  similarity_to_canonical numeric,
  source_rank integer,
  reasoning text,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE candidate_fact_dedup_group_members
ADD COLUMN IF NOT EXISTS member_role text;

ALTER TABLE candidate_fact_dedup_group_members
ADD COLUMN IF NOT EXISTS suggested_action text;

ALTER TABLE candidate_fact_dedup_group_members
ADD COLUMN IF NOT EXISTS similarity_to_canonical numeric;

ALTER TABLE candidate_fact_dedup_group_members
ADD COLUMN IF NOT EXISTS source_rank integer;

ALTER TABLE candidate_fact_dedup_group_members
ADD COLUMN IF NOT EXISTS reasoning text;

ALTER TABLE candidate_fact_dedup_group_members
ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

UPDATE candidate_fact_dedup_group_members
SET
  member_role = COALESCE(member_role, 'duplicate_candidate'),
  suggested_action = COALESCE(suggested_action, 'review_duplicate'),
  source_rank = COALESCE(source_rank, 999),
  created_at = COALESCE(created_at, now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_fact_dedup_members_group_fact
ON candidate_fact_dedup_group_members(group_id, candidate_fact_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_members_group
ON candidate_fact_dedup_group_members(group_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_members_fact
ON candidate_fact_dedup_group_members(candidate_fact_id);

DROP VIEW IF EXISTS v_candidate_fact_dedup_review;
DROP VIEW IF EXISTS v_candidate_fact_dedup_summary;

CREATE OR REPLACE VIEW v_candidate_fact_dedup_review AS
SELECT
  g.id AS group_id,
  left(g.id::text, 8) AS group_short_id,
  g.group_type,
  g.group_status,
  g.member_count,
  g.avg_similarity,
  g.max_similarity,
  g.group_confidence,
  left(g.canonical_candidate_fact_id::text, 8) AS canonical_short_id,
  g.representative_text,
  g.reasoning AS group_reasoning,

  m.member_role,
  m.suggested_action,
  m.similarity_to_canonical,
  m.source_rank,
  m.reasoning AS member_reasoning,

  cpf.id AS candidate_fact_id,
  left(cpf.id::text, 8) AS candidate_short_id,
  cpf.status AS candidate_status,
  cpf.category,
  cpf.subcategory,
  cpf.confidence AS extractor_confidence,
  cpf.fact_text,
  cpf.evidence_quote,

  rf.file_name AS source_file,
  rf.file_role,
  pc.chunk_index AS source_chunk_index,
  pc.section AS source_section,

  g.created_at
FROM candidate_fact_dedup_groups g
JOIN candidate_fact_dedup_group_members m
  ON m.group_id = g.id
JOIN candidate_profile_facts cpf
  ON cpf.id = m.candidate_fact_id
LEFT JOIN raw_files rf
  ON rf.id = cpf.source_file_id
LEFT JOIN profile_chunks pc
  ON pc.id = cpf.source_chunk_id
ORDER BY
  CASE g.group_status
    WHEN 'pending_review' THEN 1
    WHEN 'resolved' THEN 2
    WHEN 'ignored' THEN 3
    ELSE 4
  END,
  CASE g.group_type
    WHEN 'exact_duplicate' THEN 1
    WHEN 'semantic_duplicate' THEN 2
    ELSE 3
  END,
  g.member_count DESC,
  g.group_confidence DESC NULLS LAST,
  g.created_at DESC,
  g.id,
  m.source_rank;

CREATE OR REPLACE VIEW v_candidate_fact_dedup_summary AS
SELECT
  group_type,
  group_status,
  count(*) AS group_count,
  sum(member_count) AS total_members,
  round(avg(group_confidence), 3) AS avg_group_confidence
FROM candidate_fact_dedup_groups
GROUP BY group_type, group_status
ORDER BY group_type, group_status;
