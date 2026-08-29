"""Canonical profile chunk embedder.

This is the single persistence path for profile vectors.  It retains the
original architecture guarantees (verified local-profile sources, content
identity, provider/model provenance, component-run accounting) and the useful
operator/runtime capabilities that had drifted into ``embed_profile_chunks_v2``
(dry-run, retries, model override, priority ordering, savepoints and batched
commits).  The legacy ``profile_chunks.embedding`` column is mirrored for
backward compatibility, while ``profile_chunk_embeddings`` remains retrieval
authority.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402
from services.common.llm_gateway import LLMEmbeddingResult, embed_result, resolve_config  # noqa: E402
from services.common.config import database_dsn  # noqa: E402


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
EMBED_MODEL = get_model("embed")
EMBED_DIM = int(os.getenv("PROFILE_EMBED_DIM", "768"))
COMPONENT_NAME = "profile_chunk_embedder"
TASK_TYPE = "embed_profile_chunk"
VERSION = "profile_chunk_embedder_canonical_v3_2026_08_28"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def vector_literal(vec: List[float]) -> str:
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def table_columns(cur, table_name: str) -> set[str]:
    cur.execute(
        """SELECT column_name FROM information_schema.columns
             WHERE table_schema='public' AND table_name=%s;""",
        (table_name,),
    )
    return {str(row[0]) for row in cur.fetchall()}


def assert_embedding_schema(cur) -> None:
    required = {
        "chunk_id", "file_id", "embedding_model", "embedding_provider",
        "resolved_embedding_model", "embedding_dim", "content_hash",
        "embedding", "status", "error_message",
    }
    present = table_columns(cur, "profile_chunk_embeddings")
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(
            "profile_chunk_embeddings is behind the canonical vector-space schema; "
            f"missing {missing}. Apply migrations through 097 before embedding."
        )


def build_embedding_text(row) -> str:
    (_chunk_id, _file_id, file_name, file_role, chunk_index, section, category, text_content) = row
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


def embed_text_result(
    text: str,
    model: str = EMBED_MODEL,
    retries: int = 3,
    *,
    expected_dim: int = EMBED_DIM,
) -> LLMEmbeddingResult:
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            result = embed_result(texts=[text], model=model, local_url=OLLAMA_BASE_URL, timeout=180)
            if len(result.vectors) != 1:
                raise RuntimeError(
                    f"Embedding backend returned {len(result.vectors)} vectors for one chunk."
                )
            emb = result.vectors[0]
            if len(emb) != expected_dim:
                raise RuntimeError(
                    f"Embedding dim mismatch. Expected {expected_dim}, got {len(emb)}. "
                    "Change PROFILE_EMBED_DIM and vector schema before using another dimension."
                )
            return result
        except Exception as exc:
            last_error = exc
            if attempt < max(1, retries):
                time.sleep(2 * attempt)
    raise RuntimeError(f"LLM embedding failed after {max(1, retries)} retries: {last_error}") from last_error


def embed_text(text: str, model: str = EMBED_MODEL, retries: int = 3) -> List[float]:
    """Backward-compatible vector-only helper."""
    return list(embed_text_result(text, model=model, retries=retries).vectors[0])


def fetch_chunks(
    cur,
    limit: int,
    embedding_model: str = EMBED_MODEL,
    embedding_provider: str | None = None,
):
    """Return ordered eligible chunks; content freshness is checked in Python.

    Do not exclude merely because *some* row exists for provider/model: the
    structured embedding input may have changed while the chunk id stayed the
    same. The caller applies ``limit`` to non-current rows after hashing the
    exact canonical embedding text, avoiding stale-row starvation.
    """
    del limit, embedding_model, embedding_provider
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
        JOIN raw_files rf ON rf.id = pc.file_id
        WHERE rf.source = 'local_profile_ingestion'
          AND rf.is_active = true
          AND rf.path_status = 'verified'
          AND pc.text_content IS NOT NULL
          AND length(btrim(pc.text_content)) > 0
        ORDER BY
          CASE rf.file_role
            WHEN 'primary_profile_evidence' THEN 1
            WHEN 'project_artifact_evidence' THEN 2
            WHEN 'enriched_profile_evidence' THEN 3
            WHEN 'course_reference_material' THEN 4
            ELSE 5
          END,
          rf.file_name,
          pc.chunk_index;
        """,
        (),
    )
    return cur.fetchall()


def _already_current(
    cur, *, chunk_id: object, provider: str, configured_model: str,
    resolved_model: str | None, content_sha: str,
) -> bool:
    # A configured alias is not a vector-space identity. API runs establish the
    # provider-resolved model from one real embedding before any row may be
    # skipped; local Ollama has a stable configured==resolved identity.
    if not resolved_model:
        return False
    cur.execute(
        """SELECT 1 FROM profile_chunk_embeddings
             WHERE chunk_id=%s AND embedding_provider=%s AND embedding_model=%s
               AND resolved_embedding_model=%s AND content_hash=%s AND status='completed'
             LIMIT 1;""",
        (chunk_id, provider, configured_model, resolved_model, content_sha),
    )
    return cur.fetchone() is not None


def _save_success(cur, *, row, result: LLMEmbeddingResult, content_sha: str, vector: List[float]) -> object:
    chunk_id, file_id = row[0], row[1]
    literal = vector_literal(vector)
    cur.execute(
        """
        INSERT INTO profile_chunk_embeddings (
          chunk_id,file_id,embedding_provider,embedding_model,resolved_embedding_model,
          embedding_dim,content_hash,embedding,status
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,'completed')
        ON CONFLICT (chunk_id,embedding_provider,embedding_model,resolved_embedding_model,content_hash)
        DO UPDATE SET embedding=EXCLUDED.embedding,status='completed',error_message=NULL,updated_at=now()
        RETURNING id;
        """,
        (
            chunk_id, file_id, result.provider, result.configured_model, result.model,
            len(vector), content_sha, literal,
        ),
    )
    embedding_id = cur.fetchone()[0]

    # Preserve the legacy vector column as a compatibility mirror; canonical
    # retrieval never relies on it.
    cur.execute("UPDATE profile_chunks SET embedding=%s::vector WHERE id=%s;", (literal, chunk_id))

    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,task_type,source_file_id,source_chunk_id,input_json,output_json,
          status,model_provider,model_name,input_tokens,output_tokens,estimated_cost_usd,finished_at
        ) VALUES (%s,%s,%s,%s,%s,%s,'completed',%s,%s,%s,0,%s,now());
        """,
        (
            COMPONENT_NAME, TASK_TYPE, file_id, chunk_id,
            Jsonb({
                "embedder_version": VERSION,
                "embedding_model": result.configured_model,
                "resolved_model": result.model,
                "provider": result.provider,
                "embedding_dim": len(vector),
                "content_hash": content_sha,
            }),
            Jsonb({"embedding_id": str(embedding_id), "status": "completed"}),
            result.provider, result.model, result.tokens_input, result.estimated_cost_usd,
        ),
    )
    return embedding_id


def _save_failure(
    cur, *, row, provider: str, configured_model: str, resolved_model: str | None,
    content_sha: str, error: Exception,
) -> None:
    chunk_id, file_id = row[0], row[1]
    message = str(error)[:4000]
    cur.execute(
        """
        INSERT INTO profile_chunk_embeddings (
          chunk_id,file_id,embedding_provider,embedding_model,resolved_embedding_model,
          embedding_dim,content_hash,status,error_message
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'failed',%s)
        ON CONFLICT (chunk_id,embedding_provider,embedding_model,resolved_embedding_model,content_hash)
        DO UPDATE SET status='failed',error_message=EXCLUDED.error_message,updated_at=now()
        WHERE profile_chunk_embeddings.status <> 'completed';
        """,
        (chunk_id, file_id, provider, configured_model,
         resolved_model or f"unresolved:{configured_model}", EMBED_DIM, content_sha, message),
    )
    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,task_type,source_file_id,source_chunk_id,input_json,output_json,
          status,error_message,model_provider,model_name,input_tokens,output_tokens,
          estimated_cost_usd,finished_at
        ) VALUES (%s,%s,%s,%s,%s,%s,'failed',%s,%s,%s,0,0,0,now());
        """,
        (
            COMPONENT_NAME, TASK_TYPE, file_id, chunk_id,
            Jsonb({
                "embedder_version": VERSION,
                "embedding_model": configured_model,
                "provider": provider,
                "content_hash": content_sha,
            }),
            Jsonb({}), message, provider, configured_model,
        ),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonical JobOS profile chunk embedder")
    # Preserve the historical positional limit while providing V2's proper CLI.
    parser.add_argument("legacy_limit", nargs="?", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Persist embeddings (explicit apply mode).")
    mode.add_argument("--dry-run", action="store_true", help="Preview eligible/stale chunks without embedding or DB writes.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default=EMBED_MODEL)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument(
        "--write-log-table", action="store_true",
        help="Compatibility flag: canonical profile_chunk_embeddings persistence is always enabled on --apply.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    limit = args.limit if args.limit is not None else (args.legacy_limit if args.legacy_limit is not None else 100)
    if limit < 1 or args.batch_size < 1 or args.retries < 1:
        raise SystemExit("--limit, --batch-size and --retries must be positive")

    config = resolve_config(role="embed", model=args.model, local_url=OLLAMA_BASE_URL)
    print("===== PROFILE CHUNK EMBEDDER =====")
    print(f"Version: {VERSION}")
    # Preserve V1's historical default-write contract. V2's wrapper injects
    # --dry-run when its caller omits a mode, preserving V2's historical default.
    apply_mode = bool(args.apply or not args.dry_run)
    print(f"Mode: {'APPLY' if apply_mode else 'DRY-RUN'}")
    print(f"Configured model: {config.model}")
    print(f"Provider: {config.provider}")
    print(f"Expected dim: {EMBED_DIM}")
    print(f"Limit: {limit}")

    embedded = failed = skipped_current = 0
    # For token/API backends the configured model may be an alias. Establish the
    # actual vector-space identity with the first successful embedding before
    # treating any persisted row as current. This costs at most one probe call
    # on an otherwise-current API profile refresh.
    resolved_space: str | None = config.model if config.backend == "ollama" else None
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        assert_embedding_schema(cur)
        rows = fetch_chunks(cur, limit, config.model, config.provider)
        print(f"Eligible chunks scanned: {len(rows)}")
        work_items = 0
        for index, row in enumerate(rows, start=1):
            chunk_id, _file_id, file_name, _file_role, chunk_index = row[:5]
            emb_text = build_embedding_text(row)
            h = content_hash(emb_text)
            print(f"- [{index}/{len(rows)}] {file_name} chunk {chunk_index}")

            if _already_current(
                cur, chunk_id=chunk_id, provider=config.provider,
                configured_model=config.model, resolved_model=resolved_space, content_sha=h,
            ):
                skipped_current += 1
                continue
            if work_items >= limit:
                break
            work_items += 1
            if not apply_mode:
                continue

            cur.execute("SAVEPOINT chunk_embed_sp;")
            try:
                result = embed_text_result(
                    emb_text, model=args.model, retries=args.retries, expected_dim=EMBED_DIM
                )
                vector = list(result.vectors[0])
                if resolved_space is None:
                    resolved_space = result.model
                elif result.model != resolved_space:
                    raise RuntimeError(
                        "Embedding provider changed resolved model/vector space within one run: "
                        f"{resolved_space!r} -> {result.model!r}. Configure a stable concrete embedding model."
                    )
                _save_success(cur, row=row, result=result, content_sha=h, vector=vector)
                cur.execute("RELEASE SAVEPOINT chunk_embed_sp;")
                embedded += 1
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT chunk_embed_sp;")
                cur.execute("RELEASE SAVEPOINT chunk_embed_sp;")
                _save_failure(
                    cur, row=row, provider=config.provider, configured_model=config.model,
                    resolved_model=resolved_space, content_sha=h, error=exc,
                )
                failed += 1
                print(f"  ERROR: {exc}")

            if (embedded + failed) % args.batch_size == 0:
                conn.commit()
                print("  committed batch")

        if apply_mode:
            conn.commit()
        else:
            conn.rollback()

    print(f"Embedded: {embedded}")
    print(f"Failed: {failed}")
    print(f"Already current: {skipped_current}")
    if not apply_mode:
        print("Dry-run only. Re-run with --apply (or use V1's historical default) to persist canonical + legacy-compatible vectors.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
