import hashlib
import os
import sys
from pathlib import Path
from typing import List

import psycopg
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402
from services.common.llm_gateway import LLMEmbeddingResult, embed_result, resolve_config  # noqa: E402
from services.common.config import database_dsn  # noqa: E402


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
EMBED_MODEL = get_model("embed")
EMBED_DIM = int(os.getenv("PROFILE_EMBED_DIM", "768"))

COMPONENT_NAME = "profile_chunk_embedder"
TASK_TYPE = "embed_profile_chunk"

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def vector_literal(vec: List[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def build_embedding_text(row) -> str:
    (
        chunk_id,
        file_id,
        file_name,
        file_role,
        chunk_index,
        section,
        category,
        text_content,
    ) = row

    return "\n".join(
        [
            f"FILE: {file_name}",
            f"FILE_ROLE: {file_role}",
            f"CHUNK_INDEX: {chunk_index}",
            f"SECTION: {section or ''}",
            f"CATEGORY: {category or ''}",
            "",
            text_content or "",
        ]
    ).strip()


def embed_text_result(text: str) -> LLMEmbeddingResult:
    result = embed_result(
        texts=[text], model=EMBED_MODEL, local_url=OLLAMA_BASE_URL, timeout=120
    )
    if len(result.vectors) != 1:
        raise RuntimeError(f"Embedding backend returned {len(result.vectors)} vectors for one chunk.")
    emb = result.vectors[0]
    if len(emb) != EMBED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch. Expected {EMBED_DIM}, got {len(emb)}. "
            f"Change PROFILE_EMBED_DIM and migration/table if using another model."
        )
    return result


def embed_text(text: str) -> List[float]:
    """Backward-compatible vector-only helper."""
    return list(embed_text_result(text).vectors[0])


def fetch_chunks(cur, limit: int, embedding_model: str = EMBED_MODEL):
    cur.execute(
        """
        SELECT
          pc.id,
          rf.id AS file_id,
          rf.file_name,
          rf.file_role,
          pc.chunk_index,
          pc.section,
          pc.category,
          pc.text_content
        FROM profile_chunks pc
        JOIN raw_files rf
          ON rf.id = pc.file_id
        WHERE rf.source = 'local_profile_ingestion'
          AND rf.is_active = true
          AND rf.path_status = 'verified'
          AND pc.text_content IS NOT NULL
          AND length(btrim(pc.text_content)) > 0
          AND NOT EXISTS (
            SELECT 1
            FROM profile_chunk_embeddings e
            WHERE e.chunk_id = pc.id
              AND e.embedding_model = %s
              AND e.status = 'completed'
          )
        ORDER BY rf.file_name, pc.chunk_index
        LIMIT %s;
        """,
        (embedding_model, limit),
    )
    return cur.fetchall()


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) >= 2 else 100

    print("===== PROFILE CHUNK EMBEDDER =====")
    config = resolve_config(role="embed", model=EMBED_MODEL, local_url=OLLAMA_BASE_URL)
    configured_model = config.model
    print(f"Configured model: {configured_model}")
    print(f"Provider: {config.provider}")
    print(f"Dim:   {EMBED_DIM}")
    print(f"Limit: {limit}")
    print("")

    embedded = 0
    failed = 0

    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            rows = fetch_chunks(cur, limit, configured_model)
            print(f"Chunks selected: {len(rows)}")

            for row in rows:
                chunk_id = row[0]
                file_id = row[1]
                file_name = row[2]
                chunk_index = row[4]

                emb_text = build_embedding_text(row)
                h = content_hash(emb_text)

                print(f"- {file_name} chunk {chunk_index}")

                try:
                    embedding_result = embed_text_result(emb_text)
                    emb = list(embedding_result.vectors[0])

                    cur.execute(
                        """
                        INSERT INTO profile_chunk_embeddings (
                          chunk_id,
                          file_id,
                          embedding_model,
                          embedding_dim,
                          content_hash,
                          embedding,
                          status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s::vector, 'completed')
                        ON CONFLICT (chunk_id, embedding_model, content_hash)
                        DO UPDATE SET
                          embedding = EXCLUDED.embedding,
                          status = 'completed',
                          error_message = NULL,
                          updated_at = now()
                        RETURNING id;
                        """,
                        (
                            chunk_id,
                            file_id,
                            embedding_result.configured_model,
                            EMBED_DIM,
                            h,
                            vector_literal(emb),
                        ),
                    )
                    embedding_id = cur.fetchone()[0]

                    cur.execute(
                        """
                        INSERT INTO component_runs (
                          component_name,
                          task_type,
                          source_file_id,
                          source_chunk_id,
                          input_json,
                          output_json,
                          status,
                          model_provider,
                          model_name,
                          input_tokens,
                          output_tokens,
                          estimated_cost_usd,
                          finished_at
                        )
                        VALUES (
                          %s,
                          %s,
                          %s,
                          %s,
                          %s,
                          %s,
                          'completed',
                          %s,
                          %s,
                          %s,
                          0,
                          %s,
                          now()
                        );
                        """,
                        (
                            COMPONENT_NAME,
                            TASK_TYPE,
                            file_id,
                            chunk_id,
                            Jsonb(
                                {
                                    "embedding_model": embedding_result.configured_model,
                                    "resolved_model": embedding_result.model,
                                    "provider": embedding_result.provider,
                                    "embedding_dim": EMBED_DIM,
                                    "content_hash": h,
                                }
                            ),
                            Jsonb(
                                {
                                    "embedding_id": str(embedding_id),
                                    "status": "completed",
                                }
                            ),
                            embedding_result.provider,
                            embedding_result.model,
                            embedding_result.tokens_input,
                            embedding_result.estimated_cost_usd,
                        ),
                    )

                    conn.commit()
                    embedded += 1

                except Exception as e:
                    conn.rollback()
                    with conn.cursor() as err_cur:
                        err_cur.execute(
                            """
                            INSERT INTO profile_chunk_embeddings (
                              chunk_id,
                              file_id,
                              embedding_model,
                              embedding_dim,
                              content_hash,
                              status,
                              error_message
                            )
                            VALUES (%s, %s, %s, %s, %s, 'failed', %s)
                            ON CONFLICT (chunk_id, embedding_model, content_hash)
                            DO UPDATE SET
                              status = 'failed',
                              error_message = EXCLUDED.error_message,
                              updated_at = now();
                            """,
                            (
                                chunk_id,
                                file_id,
                                configured_model,
                                EMBED_DIM,
                                h,
                                str(e),
                            ),
                        )

                        err_cur.execute(
                            """
                            INSERT INTO component_runs (
                              component_name,
                              task_type,
                              source_file_id,
                              source_chunk_id,
                              input_json,
                              output_json,
                              status,
                              error_message,
                              model_provider,
                              model_name,
                              input_tokens,
                              output_tokens,
                              estimated_cost_usd,
                              finished_at
                            )
                            VALUES (
                              %s,
                              %s,
                              %s,
                              %s,
                              %s,
                              %s,
                              'failed',
                              %s,
                              %s,
                              %s,
                              0,
                              0,
                              0,
                              now()
                            );
                            """,
                            (
                                COMPONENT_NAME,
                                TASK_TYPE,
                                file_id,
                                chunk_id,
                                Jsonb({"embedding_model": configured_model, "content_hash": h}),
                                Jsonb({}),
                                str(e),
                                config.provider,
                                configured_model,
                            ),
                        )
                    conn.commit()
                    failed += 1
                    print(f"  ERROR: {e}")

    print("")
    print(f"Embedded: {embedded}")
    print(f"Failed:   {failed}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
