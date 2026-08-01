-- 025_candidate_fact_semantic_dedup.sql
-- Real semantic dedup layer for candidate_profile_facts.
-- This is the production path after Claim Extractor and before Conflict Resolver.

CREATE EXTENSION IF NOT EXISTS vector;

INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes
)
VALUES
  (
    'candidate_fact_embedder',
    'service',
    'profile',
    'Embed candidate_profile_facts for semantic deduplication and conflict detection.',
    false,
    'active',
    'Required by Profile Knowledge Layer before semantic dedup and conflict resolver.'
  ),
  (
    'semantic_dedup_worker',
    'agent',
    'profile',
    'Group exact and semantically duplicate candidate facts before conflict resolution and user approval.',
    true,
    'active',
    'Production semantic dedup worker. Does not directly approve or reject facts.'
  )
ON CONFLICT (name)
DO UPDATE SET
  component_type = EXCLUDED.component_type,
  layer = EXCLUDED.layer,
  purpose = EXCLUDED.purpose,
  trainable = EXCLUDED.trainable,
  status = EXCLUDED.status,
  notes = EXCLUDED.notes,
  updated_at = now();

CREATE TABLE IF NOT EXISTS candidate_fact_embeddings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  candidate_fact_id uuid NOT NULL REFERENCES candidate_profile_facts(id) ON DELETE CASCADE,

  embedding_model text NOT NULL,
  embedding_dim integer NOT NULL DEFAULT 768,

  content_hash text NOT NULL,
  embedding vector(768),

  status text NOT NULL DEFAULT 'completed',
  error_message text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),

  UNIQUE(candidate_fact_id, embedding_model, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_embeddings_fact
ON candidate_fact_embeddings(candidate_fact_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_embeddings_model
ON candidate_fact_embeddings(embedding_model);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_embeddings_status
ON candidate_fact_embeddings(status);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_embeddings_vector_hnsw
ON candidate_fact_embeddings
USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS candidate_fact_dedup_groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,

  dedup_version text NOT NULL,
  group_fingerprint text NOT NULL,

  group_type text NOT NULL,
  -- exact_duplicate / semantic_duplicate

  group_status text NOT NULL DEFAULT 'pending_review',
  -- pending_review / resolved / ignored / superseded

  canonical_candidate_fact_id uuid REFERENCES candidate_profile_facts(id) ON DELETE SET NULL,

  member_count integer NOT NULL DEFAULT 0,
  avg_similarity numeric,
  max_similarity numeric,
  group_confidence numeric,

  representative_text text,
  reasoning text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),

  UNIQUE(dedup_version, group_fingerprint)
);

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

  member_role text NOT NULL,
  -- canonical / duplicate_candidate

  suggested_action text NOT NULL,
  -- keep_canonical / review_duplicate / reject_exact_duplicate

  similarity_to_canonical numeric,
  source_rank integer,
  reasoning text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(group_id, candidate_fact_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_members_group
ON candidate_fact_dedup_group_members(group_id);

CREATE INDEX IF NOT EXISTS idx_candidate_fact_dedup_members_fact
ON candidate_fact_dedup_group_members(candidate_fact_id);

CREATE OR REPLACE VIEW v_candidate_fact_embedding_status AS
SELECT
  cpf.status AS candidate_status,
  count(cpf.id) AS candidate_count,
  count(e.id) FILTER (WHERE e.status = 'completed') AS embedded_count,
  count(cpf.id) - count(e.id) FILTER (WHERE e.status = 'completed') AS missing_embeddings
FROM candidate_profile_facts cpf
LEFT JOIN candidate_fact_embeddings e
  ON e.candidate_fact_id = cpf.id
  AND e.status = 'completed'
WHERE cpf.status IN ('pending', 'needs_edit', 'approved')
GROUP BY cpf.status
ORDER BY cpf.status;

-- FIXED 2026-08-01: v_candidate_fact_dedup_review already existed from
-- 010_semantic_dedup.sql with a completely different column layout
-- (group_status/group_type swapped position, several columns renamed or
-- removed). CREATE OR REPLACE VIEW cannot reorder/rename existing view
-- columns, only append new ones at the end -- confirmed live, this failed
-- a real install with "cannot change name of view column ... to ...",
-- same failure class as the 024/v_profile_retrieval_latest_results bug
-- fixed earlier the same day. DROP first, matching what
-- 025a_fix_candidate_fact_dedup_schema.sql already does for this same
-- view right after this file runs (so this is safe either way -- 025a's
-- own DROP+CREATE re-establishes the real final shape regardless).
DROP VIEW IF EXISTS v_candidate_fact_dedup_review;

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
