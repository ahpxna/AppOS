import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import psycopg
from psycopg.types.json import Jsonb


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

EMBED_MODEL = os.getenv("PROFILE_EMBED_MODEL", "nomic-embed-text")

COMPONENT_NAME = "semantic_dedup_worker"
TASK_TYPE = "dedup_candidate_profile_facts"
DEDUP_VERSION = "semantic_dedup_v2_pairwise_overlap_2026_04_27"

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)


STOPWORDS = {
    "user", "has", "have", "had", "is", "are", "was", "were", "with", "and", "or",
    "the", "for", "from", "that", "this", "into", "using", "used", "use", "in",
    "on", "of", "to", "a", "an", "skills", "skill", "coursework", "knowledge",
    "experience", "project", "projects", "worked", "working", "familiar",
    "proficient", "understands", "understand", "studying", "studied", "about",
    "related", "through", "including", "includes", "such", "as"
}


def normalize_fact(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"^user\s+", "", text)
    text = re.sub(r"[^a-z0-9+#./-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def terms(text: str) -> set:
    norm = normalize_fact(text)
    raw = re.split(r"\s+", norm)
    out = set()
    for t in raw:
        t = t.strip(".,;:()[]{}")
        if len(t) < 3:
            continue
        if t in STOPWORDS:
            continue
        out.add(t)
    return out


def lexical_match(a: str, b: str, min_shared: int, min_jaccard: float, min_containment: float) -> Tuple[bool, Dict[str, Any]]:
    ta = terms(a)
    tb = terms(b)

    if not ta or not tb:
        return False, {
            "shared_count": 0,
            "jaccard": 0.0,
            "containment": 0.0,
            "shared_terms": [],
        }

    shared = ta & tb
    union = ta | tb
    jaccard = len(shared) / max(len(union), 1)
    containment = len(shared) / max(min(len(ta), len(tb)), 1)

    ok = (
        len(shared) >= min_shared
        and (
            jaccard >= min_jaccard
            or containment >= min_containment
        )
    )

    return ok, {
        "shared_count": len(shared),
        "jaccard": round(jaccard, 4),
        "containment": round(containment, 4),
        "shared_terms": sorted(shared)[:30],
    }


def group_fingerprint(member_ids: List[str], group_type: str) -> str:
    raw = group_type + ":" + "|".join(sorted(member_ids))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_pool(cur, limit: int) -> Dict[str, Dict[str, Any]]:
    cur.execute(
        """
        SELECT
          cpf.id,
          cpf.status,
          cpf.category,
          cpf.subcategory,
          cpf.fact_text,
          cpf.evidence_quote,
          cpf.confidence,
          rf.file_name,
          rf.file_role,
          rf.evidence_weight,
          rf.allow_profile_fact_promotion,
          pc.chunk_index,
          pc.section
        FROM candidate_profile_facts cpf
        JOIN candidate_fact_embeddings e
          ON e.candidate_fact_id = cpf.id
          AND e.embedding_model = %s
          AND e.status = 'completed'
        LEFT JOIN raw_files rf
          ON rf.id = cpf.source_file_id
        LEFT JOIN profile_chunks pc
          ON pc.id = cpf.source_chunk_id
        WHERE cpf.status IN ('pending', 'needs_edit', 'approved')
          AND cpf.fact_text IS NOT NULL
          AND length(btrim(cpf.fact_text)) > 0
          AND COALESCE(rf.allow_profile_fact_promotion, false) = true
        ORDER BY cpf.created_at ASC
        LIMIT %s;
        """,
        (EMBED_MODEL, limit),
    )

    pool = {}
    for r in cur.fetchall():
        fid = str(r[0])
        pool[fid] = {
            "id": fid,
            "status": r[1],
            "category": r[2],
            "subcategory": r[3],
            "fact_text": r[4] or "",
            "evidence_quote": r[5] or "",
            "confidence": float(r[6]) if r[6] is not None else 0.0,
            "file_name": r[7],
            "file_role": r[8],
            "evidence_weight": float(r[9]) if r[9] is not None else 0.5,
            "allow_profile_fact_promotion": r[10],
            "chunk_index": r[11],
            "section": r[12],
            "normalized": normalize_fact(r[4] or ""),
        }

    return pool


def fetch_semantic_pairs(cur, threshold: float, top_k: int, limit: int) -> List[Tuple[str, str, float]]:
    cur.execute(
        """
        WITH pool AS (
          SELECT
            cpf.id AS candidate_fact_id,
            cpf.category,
            cpf.subcategory,
            e.embedding
          FROM candidate_profile_facts cpf
          JOIN candidate_fact_embeddings e
            ON e.candidate_fact_id = cpf.id
            AND e.embedding_model = %s
            AND e.status = 'completed'
          LEFT JOIN raw_files rf
            ON rf.id = cpf.source_file_id
          WHERE cpf.status IN ('pending', 'needs_edit', 'approved')
            AND cpf.fact_text IS NOT NULL
            AND length(btrim(cpf.fact_text)) > 0
            AND COALESCE(rf.allow_profile_fact_promotion, false) = true
          ORDER BY cpf.created_at ASC
          LIMIT %s
        )
        SELECT
          a.candidate_fact_id AS a_id,
          b.candidate_fact_id AS b_id,
          1 - (a.embedding <=> b.embedding) AS similarity
        FROM pool a
        JOIN LATERAL (
          SELECT
            p2.candidate_fact_id,
            p2.embedding
          FROM pool p2
          WHERE p2.candidate_fact_id::text > a.candidate_fact_id::text
            AND (
              p2.category IS NULL
              OR a.category IS NULL
              OR p2.category = a.category
            )
          ORDER BY p2.embedding <=> a.embedding
          LIMIT %s
        ) b ON true
        WHERE 1 - (a.embedding <=> b.embedding) >= %s
        ORDER BY similarity DESC;
        """,
        (EMBED_MODEL, limit, top_k, threshold),
    )

    return [(str(r[0]), str(r[1]), float(r[2])) for r in cur.fetchall()]


def choose_canonical(member_ids: List[str], pool: Dict[str, Dict[str, Any]]) -> str:
    role_score = {
        "primary_profile_evidence": 3.0,
        "project_artifact_evidence": 2.7,
        "enriched_profile_evidence": 2.2,
        "course_reference_material": 0.5,
        "career_strategy_guidance": 0.1,
        None: 0.3,
    }

    status_score = {
        "approved": 3.0,
        "pending": 2.0,
        "needs_edit": 1.0,
    }

    def score(fid: str):
        r = pool[fid]
        evidence_len = len(r.get("evidence_quote") or "")
        fact_len = len(r.get("fact_text") or "")

        return (
            role_score.get(r.get("file_role"), 0.3),
            status_score.get(r.get("status"), 0.5),
            r.get("confidence") or 0.0,
            min(evidence_len, 500) / 500,
            -abs(fact_len - 180) / 500,
        )

    return sorted(member_ids, key=score, reverse=True)[0]


def create_component_run(cur, summary: Dict[str, Any]):
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
          'deterministic_plus_pgvector',
          %s,
          0,
          0,
          0,
          now()
        )
        RETURNING id;
        """,
        (
            COMPONENT_NAME,
            TASK_TYPE,
            Jsonb(summary.get("input", {})),
            Jsonb(summary),
            json.dumps(summary, ensure_ascii=False),
            DEDUP_VERSION,
        ),
    )
    return cur.fetchone()[0]


def insert_group(
    cur,
    component_run_id,
    group_type: str,
    member_ids: List[str],
    canonical_id: str,
    pool: Dict[str, Dict[str, Any]],
    pair_meta: Dict[str, Any],
):
    fingerprint = group_fingerprint(member_ids, group_type)
    representative_text = pool[canonical_id]["fact_text"]

    similarities = pair_meta.get("similarities") or []
    avg_sim = sum(similarities) / len(similarities) if similarities else (1.0 if group_type == "exact_duplicate" else None)
    max_sim = max(similarities) if similarities else (1.0 if group_type == "exact_duplicate" else None)

    group_confidence = 0.98 if group_type == "exact_duplicate" else min(0.95, max_sim or 0.90)

    reasoning = (
        f"{group_type}: grouped {len(member_ids)} candidate facts using {DEDUP_VERSION}. "
        f"Semantic groups require vector similarity plus lexical overlap and are capped to prevent transitive over-merge."
    )

    cur.execute(
        """
        INSERT INTO candidate_fact_dedup_groups (
          component_run_id,
          dedup_version,
          group_fingerprint,
          group_type,
          group_status,
          canonical_candidate_fact_id,
          member_count,
          avg_similarity,
          max_similarity,
          group_confidence,
          representative_text,
          reasoning
        )
        VALUES (
          %s, %s, %s, %s,
          'pending_review',
          %s,
          %s,
          %s,
          %s,
          %s,
          %s,
          %s
        )
        ON CONFLICT (dedup_version, group_fingerprint)
        DO UPDATE SET
          component_run_id = EXCLUDED.component_run_id,
          group_type = EXCLUDED.group_type,
          group_status = 'pending_review',
          canonical_candidate_fact_id = EXCLUDED.canonical_candidate_fact_id,
          member_count = EXCLUDED.member_count,
          avg_similarity = EXCLUDED.avg_similarity,
          max_similarity = EXCLUDED.max_similarity,
          group_confidence = EXCLUDED.group_confidence,
          representative_text = EXCLUDED.representative_text,
          reasoning = EXCLUDED.reasoning,
          updated_at = now()
        RETURNING id;
        """,
        (
            component_run_id,
            DEDUP_VERSION,
            fingerprint,
            group_type,
            canonical_id,
            len(member_ids),
            avg_sim,
            max_sim,
            group_confidence,
            representative_text,
            reasoning,
        ),
    )
    group_id = cur.fetchone()[0]

    ordered = [canonical_id] + [fid for fid in member_ids if fid != canonical_id]

    for idx, fid in enumerate(ordered, start=1):
        is_canonical = fid == canonical_id

        if is_canonical:
            role = "canonical"
            action = "keep_canonical"
            sim_to_canonical = 1.0
            member_reasoning = "Canonical candidate selected by source role, status, confidence, and evidence length."
        else:
            role = "duplicate_candidate"
            action = "reject_exact_duplicate" if group_type == "exact_duplicate" else "review_duplicate"
            sim_to_canonical = pair_meta.get("sim_to_canonical", {}).get(fid)

            member_reasoning = json.dumps(
                {
                    "reason": "Possible duplicate of canonical candidate. No status update is applied.",
                    "lexical": pair_meta.get("lexical", {}).get(fid),
                },
                ensure_ascii=False,
            )

        cur.execute(
            """
            INSERT INTO candidate_fact_dedup_group_members (
              group_id,
              candidate_fact_id,
              member_role,
              suggested_action,
              similarity_to_canonical,
              source_rank,
              reasoning
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (group_id, candidate_fact_id)
            DO UPDATE SET
              member_role = EXCLUDED.member_role,
              suggested_action = EXCLUDED.suggested_action,
              similarity_to_canonical = EXCLUDED.similarity_to_canonical,
              source_rank = EXCLUDED.source_rank,
              reasoning = EXCLUDED.reasoning;
            """,
            (
                group_id,
                fid,
                role,
                action,
                sim_to_canonical,
                idx,
                member_reasoning,
            ),
        )

    return group_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--semantic-threshold", type=float, default=0.965)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--max-semantic-group-size", type=int, default=8)
    parser.add_argument("--min-shared-terms", type=int, default=3)
    parser.add_argument("--min-jaccard", type=float, default=0.28)
    parser.add_argument("--min-containment", type=float, default=0.60)

    args = parser.parse_args()

    print("===== SEMANTIC DEDUP CANDIDATE FACTS V2 =====")
    print(f"Version:                 {DEDUP_VERSION}")
    print(f"Limit:                   {args.limit}")
    print(f"Semantic threshold:      {args.semantic_threshold}")
    print(f"Top K:                   {args.top_k}")
    print(f"Max semantic group size: {args.max_semantic_group_size}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            pool = fetch_pool(cur, args.limit)
            print(f"Pool facts: {len(pool)}")

            exact_groups = defaultdict(list)
            for fid, row in pool.items():
                norm = row["normalized"]
                if norm:
                    exact_groups[norm].append(fid)

            exact_components = [ids for ids in exact_groups.values() if len(ids) > 1]
            exact_member_ids = {fid for group in exact_components for fid in group}

            semantic_pairs_raw = fetch_semantic_pairs(
                cur,
                threshold=args.semantic_threshold,
                top_k=args.top_k,
                limit=args.limit,
            )

            semantic_pairs = []
            rejected_by_lexical = 0

            for a, b, sim in semantic_pairs_raw:
                if a not in pool or b not in pool:
                    continue
                if a in exact_member_ids and b in exact_member_ids:
                    continue

                ok, meta = lexical_match(
                    pool[a]["fact_text"],
                    pool[b]["fact_text"],
                    min_shared=args.min_shared_terms,
                    min_jaccard=args.min_jaccard,
                    min_containment=args.min_containment,
                )

                if not ok:
                    rejected_by_lexical += 1
                    continue

                semantic_pairs.append((a, b, sim, meta))

            semantic_pairs.sort(key=lambda x: x[2], reverse=True)

            assigned = set()
            semantic_components = []

            for a, b, sim, meta in semantic_pairs:
                if a in assigned or b in assigned:
                    continue

                seed = choose_canonical([a, b], pool)
                members = [seed]
                pair_meta = {
                    "similarities": [],
                    "sim_to_canonical": {},
                    "lexical": {},
                }

                other = b if seed == a else a
                members.append(other)
                assigned.add(seed)
                assigned.add(other)

                pair_meta["similarities"].append(sim)
                pair_meta["sim_to_canonical"][other] = sim
                pair_meta["lexical"][other] = meta

                for x, y, s2, meta2 in semantic_pairs:
                    if len(members) >= args.max_semantic_group_size:
                        break

                    candidate = None
                    if x == seed and y not in assigned:
                        candidate = y
                    elif y == seed and x not in assigned:
                        candidate = x

                    if not candidate:
                        continue

                    members.append(candidate)
                    assigned.add(candidate)
                    pair_meta["similarities"].append(s2)
                    pair_meta["sim_to_canonical"][candidate] = s2
                    pair_meta["lexical"][candidate] = meta2

                if len(members) > 1:
                    semantic_components.append((members, seed, pair_meta))

            summary = {
                "input": {
                    "limit": args.limit,
                    "semantic_threshold": args.semantic_threshold,
                    "top_k": args.top_k,
                    "max_semantic_group_size": args.max_semantic_group_size,
                    "min_shared_terms": args.min_shared_terms,
                    "min_jaccard": args.min_jaccard,
                    "min_containment": args.min_containment,
                    "embedding_model": EMBED_MODEL,
                },
                "pool_facts": len(pool),
                "exact_components": len(exact_components),
                "raw_semantic_pairs": len(semantic_pairs_raw),
                "semantic_pairs_after_lexical_filter": len(semantic_pairs),
                "semantic_pairs_rejected_by_lexical": rejected_by_lexical,
                "semantic_components": len(semantic_components),
            }

            component_run_id = create_component_run(cur, summary)

            inserted_groups = 0

            for member_ids in exact_components:
                canonical_id = choose_canonical(member_ids, pool)
                pair_meta = {
                    "similarities": [1.0],
                    "sim_to_canonical": {fid: 1.0 for fid in member_ids if fid != canonical_id},
                    "lexical": {},
                }

                insert_group(
                    cur,
                    component_run_id,
                    "exact_duplicate",
                    member_ids,
                    canonical_id,
                    pool,
                    pair_meta,
                )
                inserted_groups += 1

            for member_ids, canonical_id, pair_meta in semantic_components:
                insert_group(
                    cur,
                    component_run_id,
                    "semantic_duplicate",
                    member_ids,
                    canonical_id,
                    pool,
                    pair_meta,
                )
                inserted_groups += 1

            conn.commit()

    print(f"Exact groups:          {len(exact_components)}")
    print(f"Raw semantic pairs:    {len(semantic_pairs_raw)}")
    print(f"After lexical filter:  {len(semantic_pairs)}")
    print(f"Rejected by lexical:   {rejected_by_lexical}")
    print(f"Semantic groups:       {len(semantic_components)}")
    print(f"Inserted/updated:      {inserted_groups}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
