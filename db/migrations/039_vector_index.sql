-- =========================================================
-- 039_vector_index.sql
--
-- The embedding column was declared as a bare `vector`, which has no
-- dimensions, and ivfflat requires a fixed size. nomic-embed-text emits
-- 768 dimensions, confirmed against the existing 1029 embedded rows.
--
-- Note on `lists`: the usual guidance is roughly rows/1000, so 100 is
-- generous for this table size. It is set conservatively high to stay
-- reasonable as the corpus grows; at ~1000 rows a sequential scan is
-- already fast, so the index is about future headroom rather than a
-- present bottleneck.
-- =========================================================

BEGIN;

ALTER TABLE profile_chunks
  ALTER COLUMN embedding TYPE vector(768);

CREATE INDEX IF NOT EXISTS idx_profile_chunks_embedding
  ON profile_chunks USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

COMMIT;
