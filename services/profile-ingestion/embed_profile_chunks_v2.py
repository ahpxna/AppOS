import argparse
import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import psycopg

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402
from services.common.llm_gateway import LLMEmbeddingResult, embed_result, resolve_config  # noqa: E402
from services.common.config import database_dsn  # noqa: E402


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = get_model("embed")
VERSION = "profile_chunk_embedder_v2_sources_2026_04_27"


def embed_text_result(text: str, model: str = MODEL, retries: int = 3) -> LLMEmbeddingResult:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            result = embed_result(texts=[text], model=model, local_url=OLLAMA_URL, timeout=180)
            if len(result.vectors) != 1 or not result.vectors[0]:
                raise RuntimeError("Embedding backend returned an invalid vector batch.")
            return result
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"LLM embedding failed after {retries} retries: {last_error}")


def embed_text(text: str, model: str = MODEL, retries: int = 3) -> List[float]:
    """Backward-compatible vector-only helper."""
    return list(embed_text_result(text, model=model, retries=retries).vectors[0])


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
    """Backward-compatible direct helper against the current canonical schema."""
    cols = table_columns(cur, "profile_chunk_embeddings")
    if not cols or "embedding" not in cols:
        return
    id_col = "chunk_id" if "chunk_id" in cols else ("profile_chunk_id" if "profile_chunk_id" in cols else None)
    if not id_col:
        return

    # Recover canonical chunk metadata so NOT NULL content/provider identity is
    # satisfied even for external callers of this historical helper.
    cur.execute(
        """SELECT pc.file_id,rf.file_name,coalesce(rf.file_role,''),pc.chunk_index,
                  coalesce(pc.section,''),coalesce(pc.category,''),pc.text_content
             FROM profile_chunks pc JOIN raw_files rf ON rf.id=pc.file_id
            WHERE pc.id=%s;""", (chunk_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"profile chunk not found: {chunk_id}")
    file_id, file_name, file_role, chunk_index, section, category, text_content = row
    canonical_text = "\n".join([
        f"FILE: {file_name}", f"FILE_ROLE: {file_role}", f"CHUNK_INDEX: {chunk_index}",
        f"SECTION: {section}", f"CATEGORY: {category}", "", str(text_content or ""),
    ]).strip()
    digest = hashlib.sha256(canonical_text.encode("utf-8", errors="ignore")).hexdigest()
    config = resolve_config(role="embed", model=model, local_url=OLLAMA_URL)

    values = {id_col: chunk_id, "embedding": embedding_literal}
    optional = {
        "file_id": file_id,
        "embedding_model": config.model,
        "model": config.model,
        "embedding_provider": config.provider,
        "resolved_embedding_model": config.model,
        "embedding_dim": dims,
        "dimensions": dims,
        "content_hash": digest,
        "status": "completed",
        "embedder_version": VERSION,
    }
    for key, value in optional.items():
        if key in cols:
            values[key] = value
    keys = list(values)
    placeholders = ["%s::vector" if key == "embedding" else "%s" for key in keys]
    params = [values[key] for key in keys]

    if {"embedding_provider", "embedding_model", "resolved_embedding_model", "content_hash"} <= set(values):
        cur.execute(
            f"DELETE FROM profile_chunk_embeddings WHERE {id_col}=%s AND embedding_provider=%s "
            "AND embedding_model=%s AND resolved_embedding_model=%s AND content_hash=%s",
            (chunk_id, values["embedding_provider"], values["embedding_model"],
             values["resolved_embedding_model"], values["content_hash"]),
        )
    elif "embedding_model" in values:
        cur.execute(f"DELETE FROM profile_chunk_embeddings WHERE {id_col}=%s AND embedding_model=%s", (chunk_id, values["embedding_model"]))
    else:
        cur.execute(f"DELETE FROM profile_chunk_embeddings WHERE {id_col}=%s", (chunk_id,))
    cur.execute(
        f"INSERT INTO profile_chunk_embeddings ({', '.join(keys)}) VALUES ({', '.join(placeholders)})",
        params,
    )


def _canonical_embedder():
    path = Path(__file__).with_name("embed_profile_chunks.py")
    spec = importlib.util.spec_from_file_location("jobos_profile_chunk_embedder_canonical", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load canonical embedder from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    """Compatibility CLI: V2 now routes to the canonical merged embedder.

    All historical flags remain accepted by the canonical parser, including
    --write-log-table (now a no-op because canonical log-table persistence is
    unconditional on --apply).
    """
    canonical = _canonical_embedder()
    argv = list(sys.argv[1:])
    # Preserve V2's historical default dry-run while sharing the canonical
    # implementation. Explicit --apply/--dry-run always wins.
    if "--apply" not in argv and "--dry-run" not in argv:
        argv.insert(0, "--dry-run")
    return int(canonical.main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
