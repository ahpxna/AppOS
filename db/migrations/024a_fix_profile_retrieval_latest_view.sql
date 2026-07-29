-- 024a_fix_profile_retrieval_latest_view.sql
-- Recreate retrieval latest-results view because column order changed after adding retrieval signals.

DROP VIEW IF EXISTS v_profile_retrieval_latest_results;

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
  r.rerank_score,
  r.retrieval_bucket,
  r.retrieval_signal_score,
  r.negative_retrieval_flags,
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
