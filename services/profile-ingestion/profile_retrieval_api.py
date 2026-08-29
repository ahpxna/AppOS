import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402
from services.common.llm_gateway import LLMEmbeddingResult, embed_result  # noqa: E402
from services.common.config import database_dsn  # noqa: E402


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
EMBED_MODEL = get_model("embed")
EMBED_DIM = int(os.getenv("PROFILE_EMBED_DIM", "768"))

COMPONENT_NAME = "profile_retrieval_api"
TASK_TYPE = "retrieve_profile_chunks"

def vector_literal(vec: List[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def embed_query_result(query_text: str) -> LLMEmbeddingResult:
    result = embed_result(
        texts=[query_text], model=EMBED_MODEL, local_url=OLLAMA_BASE_URL, timeout=120
    )
    if len(result.vectors) != 1:
        raise RuntimeError(f"Embedding backend returned {len(result.vectors)} vectors for one query.")
    embedding = result.vectors[0]
    if len(embedding) != EMBED_DIM:
        raise RuntimeError(f"Embedding dimension mismatch: expected {EMBED_DIM}, got {len(embedding)}")
    return result


def embed_query(query_text: str) -> List[float]:
    """Backward-compatible vector-only query embedding helper."""
    return list(embed_query_result(query_text).vectors[0])


def build_query_text(args: argparse.Namespace) -> str:
    parts = [f"Purpose: {args.purpose}"]

    if args.role_family:
        parts.append(f"Role family: {args.role_family}")

    if args.retrieval_intent:
        parts.append(f"Retrieval intent: {args.retrieval_intent}")

    if args.skills:
        parts.append("Relevant skills/topics: " + ", ".join(args.skills))

    parts.append("Query: " + args.query)
    return "\n".join(parts)


def default_excluded_buckets(intent: str) -> List[str]:
    if intent == "evidence":
        return ["guidance", "roadmap_or_future", "header_or_contact", "background_summary"]
    if intent == "mixed":
        return ["guidance", "roadmap_or_future", "header_or_contact"]
    if intent == "background":
        return ["roadmap_or_future", "header_or_contact"]
    return ["guidance", "roadmap_or_future", "header_or_contact"]


def retrieve_chunks(
    cur,
    query_embedding: List[float],
    pool_size: int,
    min_similarity: float,
    include_roles: Optional[List[str]],
    exclude_roles: Optional[List[str]],
    include_buckets: Optional[List[str]],
    exclude_buckets: Optional[List[str]],
    embedding_model: str = EMBED_MODEL,
    embedding_provider: str = "ollama",
    resolved_embedding_model: str | None = None,
) -> List[Dict[str, Any]]:
    resolved_embedding_model = resolved_embedding_model or embedding_model
    where = [
        "e.status = 'completed'",
        "e.embedding_provider = %s",
        "e.embedding_model = %s",
        "e.resolved_embedding_model = %s",
        # Retrieval is downstream of the human profile-evidence gate. A raw
        # chunk is eligible only when at least one approved profile asset cites
        # that exact chunk as evidence.
        "EXISTS (SELECT 1 FROM profile_asset_evidence_items pai "
        "JOIN profile_assets pa ON pa.id=pai.profile_asset_id "
        "WHERE pai.chunk_id=e.chunk_id AND pa.status='approved')",
    ]

    params: List[Any] = [
        vector_literal(query_embedding), embedding_provider, embedding_model, resolved_embedding_model
    ]

    if include_roles:
        where.append("s.file_role = ANY(%s)")
        params.append(include_roles)

    if exclude_roles:
        where.append("NOT (s.file_role = ANY(%s))")
        params.append(exclude_roles)

    if include_buckets:
        where.append("s.retrieval_bucket = ANY(%s)")
        params.append(include_buckets)

    if exclude_buckets:
        where.append("NOT (s.retrieval_bucket = ANY(%s))")
        params.append(exclude_buckets)

    params.extend([min_similarity, pool_size])

    sql = f"""
        WITH query_vector AS (
          SELECT %s::vector AS qvec
        )
        SELECT
          s.chunk_id,
          s.file_id,
          s.file_name,
          s.file_role,
          s.evidence_weight,
          s.chunk_index,
          s.section,
          s.category,
          s.text_content,
          s.retrieval_bucket,
          s.retrieval_signal_score,
          s.negative_retrieval_flags,
          (e.embedding <=> q.qvec) AS distance,
          (1 - (e.embedding <=> q.qvec)) AS similarity
        FROM profile_chunk_embeddings e
        JOIN v_profile_chunk_retrieval_signals s
          ON s.chunk_id = e.chunk_id
        CROSS JOIN query_vector q
        WHERE {' AND '.join(where)}
          AND (1 - (e.embedding <=> q.qvec)) >= %s
        ORDER BY
          (e.embedding <=> q.qvec) ASC
        LIMIT %s;
    """

    cur.execute(sql, params)
    rows = cur.fetchall()

    results = []
    for i, r in enumerate(rows, start=1):
        text_content = r[8] or ""
        results.append(
            {
                "rank": i,
                "chunk_id": str(r[0]),
                "file_id": str(r[1]) if r[1] else None,
                "file_name": r[2],
                "file_role": r[3],
                "evidence_weight": float(r[4]) if r[4] is not None else None,
                "chunk_index": r[5],
                "section": r[6],
                "category": r[7],
                "text_content": text_content,
                "text_preview": text_content[:700],
                "retrieval_bucket": r[9],
                "retrieval_signal_score": float(r[10]) if r[10] is not None else 0.0,
                "negative_retrieval_flags": r[11] or {},
                "distance": float(r[12]),
                "similarity": float(r[13]),
            }
        )

    return results


def tokenize_query(text: str) -> List[str]:
    stopwords = {
        "entry", "level", "role", "resume", "job", "jobs", "and", "or", "the", "with",
        "for", "to", "of", "in", "on", "a", "an", "analyst", "coursework", "projects",
        "hands", "hands-on"
    }
    raw = "".join(ch.lower() if ch.isalnum() else " " for ch in text).split()
    return [t for t in raw if len(t) >= 3 and t not in stopwords]


def compute_query_relevance(result: Dict[str, Any], query_terms: List[str]) -> float:
    if not query_terms:
        return 0.0
    blob = " ".join(
        [
            str(result.get("file_name") or ""),
            str(result.get("section") or ""),
            str(result.get("category") or ""),
            str(result.get("text_content") or ""),
        ]
    )
    # Use the same tokenizer on query and evidence so lexical relevance is
    # domain-neutral and token-exact (for example ``aws`` must not match
    # ``draws``). Provider- or role-specific keywords do not belong here.
    blob_terms = set(tokenize_query(blob))
    hits = sum(1 for term in query_terms if term in blob_terms)
    hit_ratio = hits / len(query_terms)
    return min(0.25, hit_ratio * 0.25)


def rerank_results(
    results: List[Dict[str, Any]],
    final_k: int,
    intent: str,
    query_text: str,
    max_per_file: int,
) -> List[Dict[str, Any]]:
    intent_bonus = {
        "evidence": {
            "evidence": 0.22,
            "background_summary": -0.22,
            "low_signal": -0.16,
        },
        "mixed": {
            "evidence": 0.14,
            "background_summary": 0.03,
            "low_signal": -0.08,
        },
        "background": {
            "background_summary": 0.16,
            "evidence": 0.04,
            "low_signal": -0.08,
        },
    }.get(intent, {})

    query_terms = tokenize_query(query_text)

    scored = []
    seen_chunks = set()

    for r in results:
        if r["chunk_id"] in seen_chunks:
            continue
        seen_chunks.add(r["chunk_id"])

        blob = " ".join(
            [
                str(r.get("file_name") or ""),
                str(r.get("section") or ""),
                str(r.get("category") or ""),
                str(r.get("text_content") or ""),
            ]
        ).lower()

        query_relevance = compute_query_relevance(r, query_terms)

        score = float(r["similarity"])
        score += float(r.get("retrieval_signal_score") or 0.0)
        score += intent_bonus.get(r.get("retrieval_bucket"), 0.0)
        score += query_relevance

        # Harder penalties for noisy resume meta sections.
        noisy_phrases = [
            "master resume bullet bank",
            "transferable skills demonstrated",
            "how to present tools",
            "star story",
            "good wording:",
            "resume phrase:",
            "core positioning",
            "final master positioning",
            "optional certification",
            "roadmap",
            "not current credentials",
            "future targeting",
        ]
        for phrase in noisy_phrases:
            if phrase in blob:
                score -= 0.35

        if r.get("retrieval_bucket") in {"guidance", "roadmap_or_future", "header_or_contact"}:
            score -= 0.60

        # For evidence mode, weak query relevance should not beat clearly relevant chunks.
        if intent == "evidence" and query_relevance < 0.04:
            score -= 0.25

        r["query_relevance_score"] = query_relevance
        r["rerank_score"] = score
        scored.append(r)

    scored.sort(
        key=lambda x: (
            x.get("rerank_score", 0.0),
            x.get("query_relevance_score", 0.0),
            x.get("similarity", 0.0),
            x.get("evidence_weight") or 0.0,
        ),
        reverse=True,
    )

    # Enforce source diversity so one giant resume/profile cannot dominate the pack.
    selected = []
    per_file_counts = {}

    for r in scored:
        file_id = r.get("file_id") or r.get("file_name")
        current_count = per_file_counts.get(file_id, 0)
        if current_count >= max_per_file:
            continue

        selected.append(r)
        per_file_counts[file_id] = current_count + 1

        if len(selected) >= final_k:
            break

    # If diversity was too strict, fill remaining from scored.
    if len(selected) < final_k:
        selected_ids = {r["chunk_id"] for r in selected}
        for r in scored:
            if r["chunk_id"] in selected_ids:
                continue
            selected.append(r)
            if len(selected) >= final_k:
                break

    for i, r in enumerate(selected, start=1):
        r["rank"] = i

    return selected

def save_retrieval(
    cur,
    args: argparse.Namespace,
    query_text: str,
    query_embedding: List[float],
    results: List[Dict[str, Any]],
    filters: Dict[str, Any],
    embedding_result: LLMEmbeddingResult | None = None,
):
    configured_embedding_model = embedding_result.configured_model if embedding_result else EMBED_MODEL
    resolved_embedding_model = embedding_result.model if embedding_result else configured_embedding_model
    embedding_provider = embedding_result.provider if embedding_result else "unknown"
    embedding_input_tokens = embedding_result.tokens_input if embedding_result else 0
    embedding_cost_usd = embedding_result.estimated_cost_usd if embedding_result else 0.0

    input_json = {
        "purpose": args.purpose,
        "role_family": args.role_family,
        "retrieval_intent": args.retrieval_intent,
        "query": args.query,
        "query_text": query_text,
        "skills": args.skills,
        "max_chunks": args.max_chunks,
        "min_similarity": args.min_similarity,
        "filters": filters,
    }

    output_json = {
        "selected_chunk_ids": [r["chunk_id"] for r in results],
        "result_count": len(results),
        "embedding": {
            "provider": embedding_provider,
            "configured_model": configured_embedding_model,
            "resolved_model": resolved_embedding_model,
            "dimension": len(query_embedding),
        },
        "results": [
            {
                "rank": r["rank"],
                "chunk_id": r["chunk_id"],
                "file_id": r["file_id"],
                "file_name": r["file_name"],
                "file_role": r["file_role"],
                "chunk_index": r["chunk_index"],
                "section": r["section"],
                "category": r["category"],
                "similarity": r["similarity"],
                "distance": r["distance"],
                "retrieval_bucket": r["retrieval_bucket"],
                "retrieval_signal_score": r["retrieval_signal_score"],
                "rerank_score": r["rerank_score"],
                "query_relevance_score": r.get("query_relevance_score"),
                "negative_retrieval_flags": r["negative_retrieval_flags"],
                "text_preview": r["text_preview"],
            }
            for r in results
        ],
    }

    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,
          task_type,
          input_json,
          output_json,
          output_text,
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
          %s,
          %s,
          %s,
          0,
          %s,
          now()
        )
        RETURNING id;
        """,
        (
            COMPONENT_NAME,
            TASK_TYPE,
            Jsonb(input_json),
            Jsonb(output_json),
            json.dumps(output_json, ensure_ascii=False),
            embedding_provider,
            resolved_embedding_model,
            embedding_input_tokens,
            embedding_cost_usd,
        ),
    )
    component_run_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO profile_retrieval_queries (
          component_run_id,
          purpose,
          query_text,
          role_family,
          retrieval_mode,
          embedding_model,
          embedding_provider,
          resolved_embedding_model,
          embedding_dim,
          query_embedding,
          max_chunks,
          min_similarity,
          filters_json,
          selected_chunk_ids,
          result_json,
          status
        )
        VALUES (
          %s,
          %s,
          %s,
          %s,
          'vector_signal_rerank',
          %s,
          %s,
          %s,
          %s,
          %s::vector,
          %s,
          %s,
          %s,
          %s,
          %s,
          'completed'
        )
        RETURNING id;
        """,
        (
            component_run_id,
            args.purpose,
            query_text,
            args.role_family,
            configured_embedding_model,
            embedding_provider,
            resolved_embedding_model,
            len(query_embedding),
            vector_literal(query_embedding),
            args.max_chunks,
            args.min_similarity,
            Jsonb(filters),
            Jsonb([r["chunk_id"] for r in results]),
            Jsonb(output_json),
        ),
    )
    retrieval_query_id = cur.fetchone()[0]

    for r in results:
        cur.execute(
            """
            INSERT INTO profile_retrieval_results (
              retrieval_query_id,
              chunk_id,
              file_id,
              rank,
              distance,
              similarity,
              retrieval_bucket,
              retrieval_signal_score,
              rerank_score,
              negative_retrieval_flags,
              file_name,
              file_role,
              chunk_index,
              section,
              category,
              text_preview
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (retrieval_query_id, chunk_id)
            DO UPDATE SET
              rank = EXCLUDED.rank,
              distance = EXCLUDED.distance,
              similarity = EXCLUDED.similarity,
              retrieval_bucket = EXCLUDED.retrieval_bucket,
              retrieval_signal_score = EXCLUDED.retrieval_signal_score,
              rerank_score = EXCLUDED.rerank_score,
              negative_retrieval_flags = EXCLUDED.negative_retrieval_flags,
              text_preview = EXCLUDED.text_preview;
            """,
            (
                retrieval_query_id,
                r["chunk_id"],
                r["file_id"],
                r["rank"],
                r["distance"],
                r["similarity"],
                r["retrieval_bucket"],
                r["retrieval_signal_score"],
                r["rerank_score"],
                Jsonb(r["negative_retrieval_flags"]),
                r["file_name"],
                r["file_role"],
                r["chunk_index"],
                r["section"],
                r["category"],
                r["text_preview"],
            ),
        )

    return retrieval_query_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--role-family", default=None)
    parser.add_argument("--retrieval-intent", choices=["evidence", "mixed", "background"], default="evidence")
    parser.add_argument("--skill", dest="skills", action="append", default=[])
    parser.add_argument("--max-chunks", type=int, default=20)
    parser.add_argument("--max-per-file", type=int, default=4)
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument("--include-role", action="append", default=None)
    parser.add_argument("--exclude-role", action="append", default=["career_strategy_guidance"])
    parser.add_argument("--include-bucket", action="append", default=None)
    parser.add_argument("--exclude-bucket", action="append", default=None)
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.exclude_bucket is None:
        args.exclude_bucket = default_excluded_buckets(args.retrieval_intent)

    query_text = build_query_text(args)
    filters = {
        "include_roles": args.include_role,
        "exclude_roles": args.exclude_role,
        "include_buckets": args.include_bucket,
        "exclude_buckets": args.exclude_bucket,
    }

    print("===== PROFILE RETRIEVAL API =====")
    print(f"Purpose:          {args.purpose}")
    print(f"Role family:      {args.role_family}")
    print(f"Retrieval intent: {args.retrieval_intent}")
    print(f"Default embed model: {EMBED_MODEL}")
    print(f"Max chunks:       {args.max_chunks}")
    print(f"Excluded buckets: {args.exclude_bucket}")
    print(f"Query:            {args.query}")
    print("")

    embedding_result = embed_query_result(query_text)
    query_embedding = list(embedding_result.vectors[0])
    print(f"Provider:         {embedding_result.provider}")
    print(f"Configured model: {embedding_result.configured_model}")
    print(f"Resolved model:   {embedding_result.model}")

    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            raw_results = retrieve_chunks(
                cur=cur,
                query_embedding=query_embedding,
                pool_size=max(args.max_chunks * 8, 80),
                min_similarity=args.min_similarity,
                include_roles=args.include_role,
                exclude_roles=args.exclude_role,
                include_buckets=args.include_bucket,
                exclude_buckets=args.exclude_bucket,
                embedding_model=embedding_result.configured_model,
                embedding_provider=embedding_result.provider,
                resolved_embedding_model=embedding_result.model,
            )

            results = rerank_results(
                raw_results,
                args.max_chunks,
                args.retrieval_intent,
                query_text,
                args.max_per_file,
            )

            retrieval_query_id = save_retrieval(
                cur=cur,
                args=args,
                query_text=query_text,
                query_embedding=query_embedding,
                results=results,
                filters=filters,
                embedding_result=embedding_result,
            )

        conn.commit()

    if args.json:
        print(
            json.dumps(
                {
                    "retrieval_query_id": str(retrieval_query_id),
                    "query_text": query_text,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"retrieval_query_id: {retrieval_query_id}")
        print(f"results:            {len(results)}")
        print("")
        for r in results:
            preview = r["text_preview"][:260].replace("\n", " ")
            print(
                f"{r['rank']:02d}. sim={r['similarity']:.4f} "
                f"signal={r['retrieval_signal_score']:.4f} "
                f"qrel={r.get('query_relevance_score', 0):.4f} "
                f"rerank={r['rerank_score']:.4f} "
                f"bucket={r['retrieval_bucket']} "
                f"{r['file_name']} chunk={r['chunk_index']} role={r['file_role']}"
            )
            print(f"    {preview}")
            print("")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
