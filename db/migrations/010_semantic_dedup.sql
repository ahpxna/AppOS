-- 010_semantic_dedup.sql
-- Stores semantic deduplication suggestions for candidate_profile_facts.
-- This does NOT automatically promote/reject facts.

CREATE TABLE IF NOT EXISTS candidate_fact_dedup_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,

  status text NOT NULL DEFAULT 'completed',

  input_candidate_count integer,
  output_group_count integer,

  model_provider text,
  model_name text,

  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS candidate_fact_dedup_groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  dedup_run_id uuid REFERENCES candidate_fact_dedup_runs(id) ON DELETE CASCADE,

  group_type text NOT NULL,
  -- duplicate / near_duplicate / conflict / related

  canonical_candidate_fact_id uuid REFERENCES candidate_profile_facts(id) ON DELETE SET NULL,

  status text NOT NULL DEFAULT 'pending',
  -- pending / accepted / rejected / applied

  group_reason text,
  confidence numeric,

  created_at timestamptz DEFAULT now(),
  reviewed_at timestamptz,
  review_note text
);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_groups_run
ON candidate_fact_dedup_groups(dedup_run_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_groups_status
ON candidate_fact_dedup_groups(status);

CREATE TABLE IF NOT EXISTS candidate_fact_dedup_group_members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  group_id uuid NOT NULL REFERENCES candidate_fact_dedup_groups(id) ON DELETE CASCADE,
  candidate_fact_id uuid NOT NULL REFERENCES candidate_profile_facts(id) ON DELETE CASCADE,

  member_role text NOT NULL,
  -- canonical / duplicate / conflict / related

  suggested_action text NOT NULL,
  -- keep / reject_duplicate / needs_edit / ask_user

  confidence numeric,
  reasoning text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(group_id, candidate_fact_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_group_members_group
ON candidate_fact_dedup_group_members(group_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_group_members_candidate
ON candidate_fact_dedup_group_members(candidate_fact_id);

CREATE OR REPLACE VIEW v_candidate_fact_dedup_review AS
SELECT
  g.id AS group_id,
  left(g.id::text, 8) AS group_short_id,
  g.status AS group_status,
  g.group_type,
  g.confidence AS group_confidence,
  g.group_reason,
  left(g.canonical_candidate_fact_id::text, 8) AS canonical_short_id,

  m.member_role,
  m.suggested_action,
  m.confidence AS member_confidence,
  m.reasoning AS member_reasoning,

  cpf.id AS candidate_fact_id,
  left(cpf.id::text, 8) AS candidate_short_id,
  cpf.status AS candidate_status,
  cpf.category,
  cpf.subcategory,
  cpf.fact_text,
  cpf.evidence_quote,

  rf.file_name AS source_file,
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
  g.created_at DESC,
  g.id,
  CASE m.member_role
    WHEN 'canonical' THEN 1
    WHEN 'duplicate' THEN 2
    WHEN 'conflict' THEN 3
    ELSE 4
  END,
  cpf.created_at DESC;
