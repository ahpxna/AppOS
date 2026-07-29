-- 025b_fix_candidate_fact_embedding_status_view.sql
-- Recreate candidate fact embedding status view after partial 025 migration.

CREATE EXTENSION IF NOT EXISTS vector;

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

DROP VIEW IF EXISTS v_candidate_fact_embedding_status;

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
