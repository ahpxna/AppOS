import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List

import psycopg
import requests
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
EMBED_MODEL = get_model("embed")
EMBED_DIM = int(os.getenv("PROFILE_EMBED_DIM", "768"))

COMPONENT_NAME = "profile_chunk_embedder"
TASK_TYPE = "embed_profile_chunk"

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)


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


def embed_text(text: str) -> List[float]:
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "model": EMBED_MODEL,
                "prompt": text,
            }
        ),
        timeout=120,
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"Ollama embedding error {resp.status_code}: {resp.text[:1000]}")

    data = resp.json()
    emb = data.get("embedding")
    if not isinstance(emb, list):
        raise RuntimeError(f"No embedding returned: {data}")

    if len(emb) != EMBED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch. Expected {EMBED_DIM}, got {len(emb)}. "
            f"Change PROFILE_EMBED_DIM and migration/table if using another model."
        )

    return emb


def fetch_chunks(cur, limit: int):
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
        (EMBED_MODEL, limit),
    )
    return cur.fetchall()


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) >= 2 else 100

    print("===== PROFILE CHUNK EMBEDDER =====")
    print(f"Model: {EMBED_MODEL}")
    print(f"Dim:   {EMBED_DIM}")
    print(f"Limit: {limit}")
    print("")

    embedded = 0
    failed = 0

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_chunks(cur, limit)
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
                    emb = embed_text(emb_text)

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
                            EMBED_MODEL,
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
                          'local_ollama',
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
                            Jsonb(
                                {
                                    "embedding_model": EMBED_MODEL,
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
                            EMBED_MODEL,
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
                                EMBED_MODEL,
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
                              'local_ollama',
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
                                Jsonb({"embedding_model": EMBED_MODEL, "content_hash": h}),
                                Jsonb({}),
                                str(e),
                                EMBED_MODEL,
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
