-- 021_profile_vector_index.sql
-- Real pgvector chunk index for Profile Knowledge Layer.

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
    'profile_chunk_embedder',
    'service',
    'profile',
    'Generate embeddings for profile_chunks and store them in pgvector for Profile Retrieval API and Profile Pack Builder.',
    false,
    'active',
    'Required by original Profile Knowledge Layer: chunks -> vector index -> retrieval -> context packs.'
  ),
  (
    'profile_retrieval_api',
    'service',
    'profile',
    'Retrieve relevant approved facts, chunks, and briefs for a job/message/interview context.',
    false,
    'planned',
    'Will query profile_chunk_embeddings, profile_facts, and profile_briefs.'
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

CREATE TABLE IF NOT EXISTS profile_chunk_embeddings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  chunk_id uuid NOT NULL REFERENCES profile_chunks(id) ON DELETE CASCADE,
  file_id uuid REFERENCES raw_files(id) ON DELETE CASCADE,

  embedding_model text NOT NULL,
  embedding_dim integer NOT NULL DEFAULT 768,

  content_hash text NOT NULL,
  embedding vector(768),

  status text NOT NULL DEFAULT 'completed',
  error_message text,

  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),

  UNIQUE(chunk_id, embedding_model, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_profile_chunk_embeddings_chunk
ON profile_chunk_embeddings(chunk_id);

CREATE INDEX IF NOT EXISTS idx_profile_chunk_embeddings_file
ON profile_chunk_embeddings(file_id);

CREATE INDEX IF NOT EXISTS idx_profile_chunk_embeddings_model
ON profile_chunk_embeddings(embedding_model);

CREATE INDEX IF NOT EXISTS idx_profile_chunk_embeddings_status
ON profile_chunk_embeddings(status);

CREATE INDEX IF NOT EXISTS idx_profile_chunk_embeddings_vector_hnsw
ON profile_chunk_embeddings
USING hnsw (embedding vector_cosine_ops);

CREATE OR REPLACE VIEW v_profile_vector_index_status AS
SELECT
  rf.file_name,
  rf.file_role,
  count(pc.id) AS total_chunks,
  count(e.id) FILTER (WHERE e.status = 'completed') AS embedded_chunks,
  count(pc.id) - count(e.id) FILTER (WHERE e.status = 'completed') AS missing_embeddings
FROM raw_files rf
JOIN profile_chunks pc
  ON pc.file_id = rf.id
LEFT JOIN profile_chunk_embeddings e
  ON e.chunk_id = pc.id
  AND e.status = 'completed'
WHERE rf.source = 'local_profile_ingestion'
GROUP BY rf.file_name, rf.file_role
ORDER BY missing_embeddings DESC, rf.file_name;

CREATE OR REPLACE VIEW v_profile_retrievable_chunks AS
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
  e.embedding_model,
  e.embedding_dim,
  e.embedding,
  e.created_at AS embedded_at
FROM profile_chunks pc
JOIN raw_files rf
  ON rf.id = pc.file_id
JOIN profile_chunk_embeddings e
  ON e.chunk_id = pc.id
WHERE rf.source = 'local_profile_ingestion'
  AND rf.is_active = true
  AND rf.path_status = 'verified'
  AND e.status = 'completed';
