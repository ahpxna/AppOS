-- 023_profile_retrieval_api.sql
-- Real Profile Retrieval API layer.
-- Retrieves relevant profile chunks from pgvector for future Profile Pack Builder.

INSERT INTO component_registry (
  name,
  component_type,
  layer,
  purpose,
  trainable,
  status,
  notes
)
VALUES (
  'profile_retrieval_api',
  'service',
  'profile',
  'Retrieve relevant profile chunks from pgvector using query embeddings, file roles, purpose, and filters.',
  false,
  'active',
  'Required by original architecture before Profile Pack Builder. Agents must receive selected context, not all profile files.'
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

CREATE TABLE IF NOT EXISTS profile_retrieval_queries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  component_run_id uuid REFERENCES component_runs(id) ON DELETE SET NULL,

  purpose text NOT NULL,
  query_text text NOT NULL,

  role_family text,
  retrieval_mode text NOT NULL DEFAULT 'vector',

  embedding_model text NOT NULL,
  embedding_dim integer NOT NULL DEFAULT 768,
  query_embedding vector(768),

  max_chunks integer NOT NULL DEFAULT 20,
  min_similarity numeric DEFAULT 0.00,

  filters_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  selected_chunk_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  result_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  status text NOT NULL DEFAULT 'completed',
  error_message text,

  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_retrieval_queries_purpose
ON profile_retrieval_queries(purpose);

CREATE INDEX IF NOT EXISTS idx_profile_retrieval_queries_role_family
ON profile_retrieval_queries(role_family);

CREATE INDEX IF NOT EXISTS idx_profile_retrieval_queries_status
ON profile_retrieval_queries(status);

CREATE TABLE IF NOT EXISTS profile_retrieval_results (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  retrieval_query_id uuid NOT NULL REFERENCES profile_retrieval_queries(id) ON DELETE CASCADE,

  chunk_id uuid NOT NULL REFERENCES profile_chunks(id) ON DELETE CASCADE,
  file_id uuid REFERENCES raw_files(id) ON DELETE SET NULL,

  rank integer NOT NULL,
  distance numeric NOT NULL,
  similarity numeric NOT NULL,

  file_name text,
  file_role text,
  chunk_index integer,
  section text,
  category text,

  text_preview text,

  created_at timestamptz DEFAULT now(),

  UNIQUE(retrieval_query_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_profile_retrieval_results_query
ON profile_retrieval_results(retrieval_query_id);

CREATE INDEX IF NOT EXISTS idx_profile_retrieval_results_chunk
ON profile_retrieval_results(chunk_id);

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

  q.created_at
FROM profile_retrieval_queries q
JOIN profile_retrieval_results r
  ON r.retrieval_query_id = q.id
ORDER BY q.created_at DESC, r.rank ASC;

CREATE OR REPLACE VIEW v_profile_retrieval_status AS
SELECT
  purpose,
  role_family,
  embedding_model,
  status,
  count(*) AS query_count,
  max(created_at) AS latest_query_at
FROM profile_retrieval_queries
GROUP BY purpose, role_family, embedding_model, status
ORDER BY latest_query_at DESC NULLS LAST;
