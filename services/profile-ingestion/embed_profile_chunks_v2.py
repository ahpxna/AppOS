import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional

import psycopg

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402
from services.common.llm_gateway import embed_texts  # noqa: E402
from services.common.config import database_dsn  # noqa: E402


DSN = database_dsn()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = get_model("embed")
VERSION = "profile_chunk_embedder_v2_sources_2026_04_27"


def embed_text(text: str, model: str = MODEL, retries: int = 3) -> List[float]:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            embedding = embed_texts(
                texts=[text], model=model, local_url=OLLAMA_URL, timeout=180
            )[0]
            if not embedding:
                raise RuntimeError("Embedding backend returned an empty vector.")
            return [float(value) for value in embedding]
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"LLM embedding failed after {retries} retries: {last_error}")


def vector_literal(values: List[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in values) + "]"


def table_columns(cur, table_name: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (table_name,),
    )
    return {r[0] for r in cur.fetchall()}


def fetch_chunks(cur, limit: int):
    cur.execute(
        """
        SELECT
          pc.id,
          rf.file_name,
          pc.chunk_index,
          pc.section,
          pc.category,
          pc.token_count,
          pc.text_content
        FROM profile_chunks pc
        JOIN raw_files rf
          ON rf.id = pc.file_id
        WHERE pc.embedding IS NULL
          AND pc.text_content IS NOT NULL
          AND btrim(pc.text_content) <> ''
        ORDER BY
          CASE rf.file_role
            WHEN 'primary_profile_evidence' THEN 1
            WHEN 'project_artifact_evidence' THEN 2
            WHEN 'enriched_profile_evidence' THEN 3
            WHEN 'course_reference_material' THEN 4
            ELSE 5
          END,
          rf.file_name,
          pc.chunk_index
        LIMIT %s
        """,
        (limit,),
    )
    return cur.fetchall()


def insert_embedding_log_if_possible(cur, chunk_id, embedding_literal: str, model: str, dims: int):
    cols = table_columns(cur, "profile_chunk_embeddings")
    if not cols:
        return

    values = {}

    if "chunk_id" in cols:
        values["chunk_id"] = chunk_id
    elif "profile_chunk_id" in cols:
        values["profile_chunk_id"] = chunk_id
    else:
        return

    if "embedding" not in cols:
        return

    values["embedding"] = embedding_literal

    if "embedding_model" in cols:
        values["embedding_model"] = model
    if "model" in cols:
        values["model"] = model
    if "embedding_dim" in cols:
        values["embedding_dim"] = dims
    if "dimensions" in cols:
        values["dimensions"] = dims
    if "status" in cols:
        values["status"] = "completed"
    if "embedder_version" in cols:
        values["embedder_version"] = VERSION

    keys = list(values.keys())
    placeholders = []
    params = []

    for k in keys:
        if k == "embedding":
            placeholders.append("%s::vector")
        else:
            placeholders.append("%s")
        params.append(values[k])

    id_col = "chunk_id" if "chunk_id" in values else "profile_chunk_id"
    model_col = "embedding_model" if "embedding_model" in values else ("model" if "model" in values else None)

    if model_col:
        cur.execute(
            f"DELETE FROM profile_chunk_embeddings WHERE {id_col} = %s AND {model_col} = %s",
            (chunk_id, model),
        )
    else:
        cur.execute(
            f"DELETE FROM profile_chunk_embeddings WHERE {id_col} = %s",
            (chunk_id,),
        )

    q = (
        f"INSERT INTO profile_chunk_embeddings ({', '.join(keys)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    cur.execute(q, params)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--write-log-table", action="store_true", help="Also write profile_chunk_embeddings log table.")
    args = parser.parse_args()

    print("===== PROFILE CHUNK EMBEDDER V2 =====")
    print(f"Version: {VERSION}")
    print(f"Mode:    {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Model:   {args.model}")
    print(f"Limit:   {args.limit}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_chunks(cur, args.limit)

            print(f"Chunks selected: {len(rows)}")

            if not rows:
                print("Nothing to embed.")
                return 0

            embedded = 0
            failed = 0

            for i, row in enumerate(rows, start=1):
                chunk_id, file_name, chunk_index, section, category, token_count, text_content = row

                print("")
                print(f"--- Chunk {i}/{len(rows)} ---")
                print(f"File:     {file_name}")
                print(f"Index:    {chunk_index}")
                print(f"Section:  {section}")
                print(f"Category: {category}")
                print(f"Tokens:   {token_count}")

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT chunk_embed_sp")
                try:
                    emb = embed_text(text_content, model=args.model)
                    lit = vector_literal(emb)

                    cur.execute(
                        """
                        UPDATE profile_chunks
                        SET embedding = %s::vector
                        WHERE id = %s
                        """,
                        (lit, chunk_id),
                    )

                    if args.write_log_table:
                        insert_embedding_log_if_possible(cur, chunk_id, lit, args.model, len(emb))

                    cur.execute("RELEASE SAVEPOINT chunk_embed_sp")

                    embedded += 1
                    print(f"Embedded dim: {len(emb)}")

                    if embedded % 25 == 0:
                        conn.commit()
                        print("Committed batch.")

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT chunk_embed_sp")
                    cur.execute("RELEASE SAVEPOINT chunk_embed_sp")
                    failed += 1
                    print(f"FAILED: {e}")

            if args.apply:
                conn.commit()

    print("")
    print("===== SUMMARY =====")
    print(f"Selected: {len(rows)}")
    print(f"Embedded: {embedded}")
    print(f"Failed:   {failed}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply to write DB.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
