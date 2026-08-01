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

COMPONENT_NAME = "candidate_fact_embedder"
TASK_TYPE = "embed_candidate_fact"

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
        candidate_fact_id,
        status,
        category,
        subcategory,
        fact_text,
        evidence_quote,
        source_file,
        file_role,
    ) = row

    return "\n".join(
        [
            f"STATUS: {status}",
            f"CATEGORY: {category or ''}",
            f"SUBCATEGORY: {subcategory or ''}",
            f"SOURCE_FILE: {source_file or ''}",
            f"FILE_ROLE: {file_role or ''}",
            "",
            f"FACT: {fact_text or ''}",
            "",
            f"EVIDENCE: {evidence_quote or ''}",
        ]
    ).strip()


def embed_text(text: str) -> List[float]:
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"model": EMBED_MODEL, "prompt": text}),
        timeout=120,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Ollama embedding error {response.status_code}: {response.text[:1000]}")

    data = response.json()
    embedding = data.get("embedding")

    if not isinstance(embedding, list):
        raise RuntimeError(f"No embedding returned: {data}")

    if len(embedding) != EMBED_DIM:
        raise RuntimeError(f"Embedding dimension mismatch: expected {EMBED_DIM}, got {len(embedding)}")

    return embedding


def fetch_candidate_facts(cur, limit: int):
    cur.execute(
        """
        SELECT
          cpf.id,
          cpf.status,
          cpf.category,
          cpf.subcategory,
          cpf.fact_text,
          cpf.evidence_quote,
          rf.file_name,
          rf.file_role
        FROM candidate_profile_facts cpf
        LEFT JOIN raw_files rf
          ON rf.id = cpf.source_file_id
        WHERE cpf.status IN ('pending', 'needs_edit', 'approved')
          AND cpf.fact_text IS NOT NULL
          AND length(btrim(cpf.fact_text)) > 0
          AND NOT EXISTS (
            SELECT 1
            FROM candidate_fact_embeddings e
            WHERE e.candidate_fact_id = cpf.id
              AND e.embedding_model = %s
              AND e.status = 'completed'
          )
        ORDER BY cpf.created_at ASC
        LIMIT %s;
        """,
        (EMBED_MODEL, limit),
    )
    return cur.fetchall()


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) >= 2 else 500

    print("===== CANDIDATE FACT EMBEDDER =====")
    print(f"Model: {EMBED_MODEL}")
    print(f"Dim:   {EMBED_DIM}")
    print(f"Limit: {limit}")
    print("")

    embedded = 0
    failed = 0

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_candidate_facts(cur, limit)
            print(f"Candidate facts selected: {len(rows)}")

            for row in rows:
                candidate_fact_id = row[0]
                fact_preview = (row[4] or "")[:120].replace("\n", " ")
                embedding_text = build_embedding_text(row)
                h = content_hash(embedding_text)

                print(f"- {str(candidate_fact_id)[:8]} {fact_preview}")

                try:
                    emb = embed_text(embedding_text)

                    cur.execute(
                        """
                        INSERT INTO candidate_fact_embeddings (
                          candidate_fact_id,
                          embedding_model,
                          embedding_dim,
                          content_hash,
                          embedding,
                          status
                        )
                        VALUES (%s, %s, %s, %s, %s::vector, 'completed')
                        ON CONFLICT (candidate_fact_id, embedding_model, content_hash)
                        DO UPDATE SET
                          embedding = EXCLUDED.embedding,
                          status = 'completed',
                          error_message = NULL,
                          updated_at = now()
                        RETURNING id;
                        """,
                        (
                            candidate_fact_id,
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
                          source_candidate_fact_id,
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
                            candidate_fact_id,
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
                            INSERT INTO candidate_fact_embeddings (
                              candidate_fact_id,
                              embedding_model,
                              embedding_dim,
                              content_hash,
                              status,
                              error_message
                            )
                            VALUES (%s, %s, %s, %s, 'failed', %s)
                            ON CONFLICT (candidate_fact_id, embedding_model, content_hash)
                            DO UPDATE SET
                              status = 'failed',
                              error_message = EXCLUDED.error_message,
                              updated_at = now();
                            """,
                            (
                                candidate_fact_id,
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
                              source_candidate_fact_id,
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
                                candidate_fact_id,
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
