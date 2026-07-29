import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional

import psycopg
import requests
from psycopg.types.json import Jsonb


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "llama3.2:3b")

COMPONENT_NAME = "profile_fact_verifier_rewriter"
TASK_TYPE = "verify_and_rewrite_candidate_fact"
VERIFIER_VERSION = "local_ollama_evidence_verifier_v1_2026_04_27"

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["approve_as_is", "rewrite", "reject", "ask_user"],
        },
        "suggested_category": {"type": "string"},
        "suggested_subcategory": {"type": "string"},
        "suggested_fact_text": {"type": "string"},
        "suggested_evidence_quote": {"type": "string"},
        "evidence_assessment": {"type": "string"},
        "context_assessment": {"type": "string"},
        "reasoning": {"type": "string"},
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confidence": {"type": "number"},
    },
    "required": [
        "decision",
        "suggested_category",
        "suggested_subcategory",
        "suggested_fact_text",
        "suggested_evidence_quote",
        "evidence_assessment",
        "context_assessment",
        "reasoning",
        "risk_flags",
        "confidence",
    ],
}


SYSTEM_PROMPT = """You are the Profile Fact Verifier + Rewriter for a job-application operating system.

You verify candidate profile facts before they become trusted profile_facts.

Return JSON only.

You receive:
- a candidate fact
- its evidence_quote
- source file metadata
- source chunk text
- neighboring chunk context

Your job:
1. Check whether the fact is directly supported by the evidence and context.
2. Detect overclaims, title/citation confusion, role confusion, future/guidance language, and weak evidence.
3. If the candidate fact is mostly correct but overstated, rewrite it into a narrower evidence-grounded fact.
4. If the evidence does not support the fact, reject it.
5. If the evidence is unclear, ask_user.
6. Do not invent new achievements, employers, memberships, tools, certifications, jobs, or degrees.
7. Do not turn career advice, role titles, citations, bibliography entries, or recommended skills into user experience.
8. Prefer conservative, resume-safe facts.

Decision rules:
- approve_as_is: fact is directly supported and wording is safe.
- rewrite: evidence supports a narrower or cleaner fact.
- reject: evidence does not support the fact, or it is clearly a citation/title/advice/source-material hallucination.
- ask_user: evidence is ambiguous and human confirmation is needed.

Important examples:
- If fact says user is a member of an external research team, but evidence is only a citation author, reject.
- If fact says user has experience with a tool, but evidence only says the tool is recommended or role-aligned, rewrite or reject.
- If fact says coursework but evidence describes a project, rewrite to project wording.
- If fact says skill broadly, but evidence supports a course/project exposure, rewrite to course/project exposure.
- If evidence contains 'should', 'recommend', 'nên', 'bổ sung', treat it as guidance unless context clearly says user already did it.
"""


def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.replace("\x00", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_ollama_response(data: Dict[str, Any]) -> Dict[str, Any]:
    content = data.get("message", {}).get("content", "")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"```$", "", content).strip()
    return json.loads(content)


def fetch_candidates(cur, limit: int, quality_bucket: Optional[str], only_ids: List[str]) -> List[Dict[str, Any]]:
    params: List[Any] = [VERIFIER_VERSION]
    where_parts = [
        "q.status IN ('pending', 'needs_edit')",
        "NOT EXISTS (SELECT 1 FROM candidate_fact_verification_suggestions s WHERE s.candidate_fact_id = q.id AND s.verifier_version = %s)",
    ]

    if quality_bucket:
        where_parts.append("q.quality_bucket = %s")
        params.append(quality_bucket)

    if only_ids:
        where_parts.append("(q.id::text = ANY(%s) OR left(q.id::text, 8) = ANY(%s))")
        params.append(only_ids)
        params.append(only_ids)

    params.append(limit)

    sql = f"""
        SELECT
          q.id,
          q.category,
          q.subcategory,
          q.fact_text,
          q.evidence_quote,
          q.reasoning,
          q.confidence,
          q.quality_bucket,
          q.source_file,
          q.file_role,
          q.source_chunk_index,
          q.source_section,
          cpf.source_file_id,
          cpf.source_chunk_id,
          rf.original_local_path,
          rf.parsed_text_path,
          rf.file_size_bytes,
          rf.path_status
        FROM v_candidate_fact_quality_review q
        JOIN candidate_profile_facts cpf
          ON cpf.id = q.id
        LEFT JOIN raw_files rf
          ON rf.id = cpf.source_file_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY
          CASE
            WHEN q.quality_bucket = 'human_review_high_priority' THEN 1
            WHEN q.quality_bucket = 'human_review_normal' THEN 2
            ELSE 3
          END,
          q.confidence DESC NULLS LAST,
          q.created_at DESC
        LIMIT %s;
    """

    cur.execute(sql, params)
    rows = cur.fetchall()

    out = []
    for r in rows:
        out.append(
            {
                "candidate_fact_id": str(r[0]),
                "category": r[1],
                "subcategory": r[2],
                "fact_text": r[3],
                "evidence_quote": r[4],
                "reasoning": r[5],
                "confidence": float(r[6]) if r[6] is not None else None,
                "quality_bucket": r[7],
                "source_file": r[8],
                "file_role": r[9],
                "source_chunk_index": r[10],
                "source_section": r[11],
                "source_file_id": str(r[12]) if r[12] else None,
                "source_chunk_id": str(r[13]) if r[13] else None,
                "original_local_path": r[14],
                "parsed_text_path": r[15],
                "file_size_bytes": r[16],
                "path_status": r[17],
            }
        )
    return out


def fetch_context(cur, source_chunk_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT
          pc.id,
          pc.file_id,
          pc.chunk_index,
          pc.section,
          pc.text_content
        FROM profile_chunks pc
        WHERE pc.id = %s;
        """,
        (source_chunk_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"source_chunk": "", "neighbor_chunks": []}

    _chunk_id, file_id, chunk_index, section, text_content = row

    cur.execute(
        """
        SELECT
          chunk_index,
          section,
          text_content
        FROM profile_chunks
        WHERE file_id = %s
          AND chunk_index BETWEEN %s AND %s
        ORDER BY chunk_index;
        """,
        (file_id, chunk_index - 1, chunk_index + 1),
    )
    neighbors = cur.fetchall()

    return {
        "source_chunk_index": chunk_index,
        "source_section": section,
        "source_chunk": clean_text(text_content),
        "neighbor_chunks": [
            {
                "chunk_index": n[0],
                "section": n[1],
                "text": clean_text(n[2]),
            }
            for n in neighbors
        ],
    }


def call_llm(candidate: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "candidate": {
            "category": candidate["category"],
            "subcategory": candidate["subcategory"],
            "fact_text": candidate["fact_text"],
            "evidence_quote": candidate["evidence_quote"],
            "extractor_reasoning": candidate["reasoning"],
            "extractor_confidence": candidate["confidence"],
            "quality_bucket": candidate["quality_bucket"],
        },
        "source": {
            "source_file": candidate["source_file"],
            "file_role": candidate["file_role"],
            "source_chunk_index": candidate["source_chunk_index"],
            "source_section": candidate["source_section"],
            "original_local_path": candidate.get("original_local_path"),
            "parsed_text_path": candidate.get("parsed_text_path"),
            "file_size_bytes": candidate.get("file_size_bytes"),
            "path_status": candidate.get("path_status"),
        },
        "context": context,
    }

    body = {
        "model": LOCAL_LLM_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "format": OUTPUT_SCHEMA,
        "options": {
            "temperature": 0,
            "top_p": 0.9,
        },
    }

    resp = requests.post(
        f"{LOCAL_LLM_BASE_URL}/api/chat",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=180,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text[:1200]}")

    data = resp.json()
    parsed = parse_ollama_response(data)
    parsed["_meta"] = {
        "model": data.get("model") or LOCAL_LLM_MODEL,
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
    }
    return parsed


def normalize_result(candidate: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    decision = result.get("decision")
    if decision not in {"approve_as_is", "rewrite", "reject", "ask_user"}:
        decision = "ask_user"

    suggested_category = clean_text(result.get("suggested_category")) or candidate["category"] or "general"
    suggested_subcategory = clean_text(result.get("suggested_subcategory")) or candidate["subcategory"] or "general"
    suggested_fact_text = clean_text(result.get("suggested_fact_text"))
    suggested_evidence_quote = clean_text(result.get("suggested_evidence_quote"))

    if decision == "approve_as_is":
        suggested_fact_text = suggested_fact_text or clean_text(candidate["fact_text"])
        suggested_evidence_quote = suggested_evidence_quote or clean_text(candidate["evidence_quote"])

    if decision == "rewrite" and not suggested_fact_text:
        decision = "ask_user"

    if decision in {"approve_as_is", "rewrite"} and not suggested_evidence_quote:
        suggested_evidence_quote = clean_text(candidate["evidence_quote"])

    try:
        confidence = float(result.get("confidence", 0.5))
    except Exception:
        confidence = 0.5

    confidence = max(0.0, min(1.0, confidence))

    risk_flags = result.get("risk_flags") or []
    if not isinstance(risk_flags, list):
        risk_flags = [str(risk_flags)]

    return {
        "decision": decision,
        "suggested_category": suggested_category,
        "suggested_subcategory": suggested_subcategory,
        "suggested_fact_text": suggested_fact_text,
        "suggested_evidence_quote": suggested_evidence_quote,
        "evidence_assessment": clean_text(result.get("evidence_assessment")),
        "context_assessment": clean_text(result.get("context_assessment")),
        "reasoning": clean_text(result.get("reasoning")),
        "risk_flags": risk_flags,
        "confidence": confidence,
        "_meta": result.get("_meta") or {},
    }


def insert_component_run(cur, candidate: Dict[str, Any], context: Dict[str, Any], result: Dict[str, Any], status: str, error: Optional[str] = None):
    input_json = {
        "candidate": candidate,
        "context": context,
        "verifier_version": VERIFIER_VERSION,
    }
    output_json = result if result else {}

    meta = result.get("_meta") if result else {}

    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,
          task_type,
          source_file_id,
          source_chunk_id,
          source_candidate_fact_id,
          input_json,
          output_json,
          output_text,
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
          %s, %s, %s, %s, %s,
          %s, %s, %s,
          %s, %s,
          'local_ollama',
          %s,
          %s,
          %s,
          0,
          now()
        )
        RETURNING id;
        """,
        (
            COMPONENT_NAME,
            TASK_TYPE,
            candidate["source_file_id"],
            candidate["source_chunk_id"],
            candidate["candidate_fact_id"],
            Jsonb(input_json),
            Jsonb(output_json),
            result.get("suggested_fact_text") if result else None,
            status,
            error,
            meta.get("model") or LOCAL_LLM_MODEL,
            meta.get("prompt_eval_count"),
            meta.get("eval_count"),
        ),
    )
    return cur.fetchone()[0]


def insert_suggestion(cur, component_run_id, candidate: Dict[str, Any], result: Dict[str, Any]):
    cur.execute(
        """
        INSERT INTO candidate_fact_verification_suggestions (
          component_run_id,
          candidate_fact_id,
          source_file_id,
          source_chunk_id,
          verifier_version,
          status,
          decision,
          original_category,
          original_subcategory,
          original_fact_text,
          original_evidence_quote,
          suggested_category,
          suggested_subcategory,
          suggested_fact_text,
          suggested_evidence_quote,
          evidence_assessment,
          context_assessment,
          reasoning,
          risk_flags,
          confidence
        )
        VALUES (
          %s, %s, %s, %s, %s,
          'pending',
          %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s,
          %s,
          %s
        )
        ON CONFLICT (candidate_fact_id, verifier_version)
        DO NOTHING
        RETURNING id;
        """,
        (
            component_run_id,
            candidate["candidate_fact_id"],
            candidate["source_file_id"],
            candidate["source_chunk_id"],
            VERIFIER_VERSION,
            result["decision"],
            candidate["category"],
            candidate["subcategory"],
            candidate["fact_text"],
            candidate["evidence_quote"],
            result["suggested_category"],
            result["suggested_subcategory"],
            result["suggested_fact_text"],
            result["suggested_evidence_quote"],
            result["evidence_assessment"],
            result["context_assessment"],
            result["reasoning"],
            Jsonb(result["risk_flags"]),
            result["confidence"],
        ),
    )
    row = cur.fetchone()
    return row[0] if row else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--quality-bucket", default="human_review_high_priority")
    parser.add_argument("--id", action="append", default=[])
    args = parser.parse_args()

    print("===== PROFILE FACT VERIFIER + REWRITER =====")
    print(f"Model:          {LOCAL_LLM_MODEL}")
    print(f"Verifier:       {VERIFIER_VERSION}")
    print(f"Limit:          {args.limit}")
    print(f"Quality bucket: {args.quality_bucket}")
    if args.id:
        print(f"IDs:            {args.id}")
    print("")

    processed = 0
    inserted = 0
    failed = 0

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            candidates = fetch_candidates(cur, args.limit, args.quality_bucket, args.id)
            print(f"Candidates selected: {len(candidates)}")

            for c in candidates:
                short_id = c["candidate_fact_id"][:8]
                print(f"\n--- Candidate {short_id} ---")
                print(f"Fact: {c['fact_text']}")
                print(f"Evidence: {c['evidence_quote']}")

                context = fetch_context(cur, c["source_chunk_id"])

                try:
                    raw = call_llm(c, context)
                    result = normalize_result(c, raw)
                    run_id = insert_component_run(cur, c, context, result, "completed")
                    suggestion_id = insert_suggestion(cur, run_id, c, result)
                    conn.commit()

                    processed += 1
                    if suggestion_id:
                        inserted += 1

                    print(f"Decision: {result['decision']}")
                    print(f"Suggested: {result['suggested_fact_text']}")
                    print(f"Suggestion: {str(suggestion_id)[:8] if suggestion_id else 'already_exists'}")

                except Exception as e:
                    conn.rollback()
                    with conn.cursor() as err_cur:
                        context_for_error = context if "context" in locals() else {}
                        run_id = insert_component_run(err_cur, c, context_for_error, {}, "failed", str(e))
                    conn.commit()
                    failed += 1
                    print(f"ERROR: {e}")

    print("")
    print(f"Processed: {processed}")
    print(f"Inserted suggestions: {inserted}")
    print(f"Failed: {failed}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
