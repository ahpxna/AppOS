import json
import os
import re
import sys
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

COMPONENT_NAME = "semantic_dedup_worker"
TASK_TYPE = "deduplicate_candidate_facts"

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)


DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group_type": {
                        "type": "string",
                        "enum": ["duplicate", "near_duplicate", "conflict", "related"],
                    },
                    "canonical_candidate_id": {"type": "string"},
                    "confidence": {"type": "number"},
                    "group_reason": {"type": "string"},
                    "members": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "candidate_id": {"type": "string"},
                                "member_role": {
                                    "type": "string",
                                    "enum": ["canonical", "duplicate", "conflict", "related"],
                                },
                                "suggested_action": {
                                    "type": "string",
                                    "enum": ["keep", "reject_duplicate", "needs_edit", "ask_user"],
                                },
                                "confidence": {"type": "number"},
                                "reasoning": {"type": "string"},
                            },
                            "required": [
                                "candidate_id",
                                "member_role",
                                "suggested_action",
                                "confidence",
                                "reasoning",
                            ],
                        },
                    },
                },
                "required": [
                    "group_type",
                    "canonical_candidate_id",
                    "confidence",
                    "group_reason",
                    "members",
                ],
            },
        }
    },
    "required": ["groups"],
}


SYSTEM_PROMPT = """You are a semantic deduplication worker for candidate profile facts.

Return JSON only.

Task:
Group candidate profile facts that are duplicates, near-duplicates, conflicts, or strongly related.

Definitions:
- duplicate: same meaning and one can be safely rejected as duplicate.
- near_duplicate: similar meaning but evidence or wording differs; may need human choice or merge.
- conflict: facts cannot both be true, or evidence suggests one is wrong.
- related: related but not necessarily duplicate; group only if useful for review.

Rules:
- Do not invent new facts.
- Do not promote, reject, or edit anything directly.
- Only group facts that clearly need review together.
- If facts are unrelated, leave them out.
- Each group must have at least 2 members.
- Choose canonical_candidate_id as the best-supported, clearest fact.
- If no groups are needed, return {"groups": []}.
- Prefer smaller high-confidence groups over large vague groups.
- Evidence matters: if the fact text says more than the evidence supports, mark needs_edit or conflict.
"""


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_ollama_message_content(response_json: Dict[str, Any]) -> Dict[str, Any]:
    content = response_json.get("message", {}).get("content", "")
    if not content:
        raise ValueError(f"No message.content in Ollama response: {response_json}")

    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"```$", "", content).strip()

    return json.loads(content)


def fetch_candidates(conn, limit: int) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              cpf.id,
              left(cpf.id::text, 8) AS short_id,
              cpf.status,
              cpf.category,
              cpf.subcategory,
              cpf.fact_text,
              cpf.evidence_quote,
              cpf.confidence,
              rf.file_name,
              pc.chunk_index,
              pc.section
            FROM candidate_profile_facts cpf
            LEFT JOIN raw_files rf
              ON rf.id = cpf.source_file_id
            LEFT JOIN profile_chunks pc
              ON pc.id = cpf.source_chunk_id
            WHERE cpf.status IN ('pending', 'needs_edit', 'approved')
            ORDER BY
              CASE cpf.status
                WHEN 'pending' THEN 1
                WHEN 'needs_edit' THEN 2
                WHEN 'approved' THEN 3
                ELSE 4
              END,
              cpf.created_at DESC
            LIMIT %s;
            """,
            (limit,),
        )
        rows = cur.fetchall()

    candidates = []
    for r in rows:
        candidates.append(
            {
                "candidate_id": str(r[0]),
                "short_id": r[1],
                "status": r[2],
                "category": r[3],
                "subcategory": r[4],
                "fact_text": r[5],
                "evidence_quote": r[6],
                "confidence": float(r[7]) if r[7] is not None else None,
                "source_file": r[8],
                "source_chunk_index": r[9],
                "source_section": r[10],
            }
        )
    return candidates


def call_ollama(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    body = {
        "model": LOCAL_LLM_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "Deduplicate these candidate profile facts.",
                        "schema": DEDUP_SCHEMA,
                        "candidates": candidates,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "format": DEDUP_SCHEMA,
        "options": {
            "temperature": 0,
            "top_p": 0.9,
        },
    }

    response = requests.post(
        f"{LOCAL_LLM_BASE_URL}/api/chat",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=180,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Ollama error {response.status_code}: {response.text[:1200]}")

    data = response.json()
    parsed = parse_ollama_message_content(data)
    parsed["_ollama_meta"] = {
        "model": data.get("model"),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
    }
    return parsed


def resolve_candidate_id(value: str, candidates_by_id: Dict[str, Dict[str, Any]], candidates_by_short: Dict[str, Dict[str, Any]]) -> Optional[str]:
    if not value:
        return None

    value = value.strip()

    if value in candidates_by_id:
        return value

    if value in candidates_by_short:
        return candidates_by_short[value]["candidate_id"]

    # Sometimes model returns shortened uuid plus text.
    prefix = value[:8]
    if prefix in candidates_by_short:
        return candidates_by_short[prefix]["candidate_id"]

    return None


def insert_results(conn, candidates: List[Dict[str, Any]], result: Dict[str, Any]) -> None:
    candidates_by_id = {c["candidate_id"]: c for c in candidates}
    candidates_by_short = {c["short_id"]: c for c in candidates}

    meta = result.get("_ollama_meta") or {}
    groups = result.get("groups") or []

    input_json = {"candidates": candidates}
    output_json = {"groups": groups}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO component_runs (
              component_name,
              task_type,
              input_json,
              output_json,
              status,
              model_provider,
              model_name,
              input_tokens,
              output_tokens,
              finished_at
            )
            VALUES (
              %s, %s, %s, %s,
              'completed',
              'local_ollama',
              %s,
              %s,
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
                meta.get("model") or LOCAL_LLM_MODEL,
                meta.get("prompt_eval_count"),
                meta.get("eval_count"),
            ),
        )
        component_run_id = cur.fetchone()[0]

        valid_groups = 0
        cur.execute(
            """
            INSERT INTO candidate_fact_dedup_runs (
              component_run_id,
              status,
              input_candidate_count,
              output_group_count,
              model_provider,
              model_name
            )
            VALUES (%s, 'completed', %s, 0, 'local_ollama', %s)
            RETURNING id;
            """,
            (
                component_run_id,
                len(candidates),
                meta.get("model") or LOCAL_LLM_MODEL,
            ),
        )
        dedup_run_id = cur.fetchone()[0]

        for group in groups:
            members = group.get("members") or []
            if len(members) < 2:
                continue

            canonical_id = resolve_candidate_id(
                str(group.get("canonical_candidate_id", "")),
                candidates_by_id,
                candidates_by_short,
            )

            resolved_members = []
            for member in members:
                cid = resolve_candidate_id(
                    str(member.get("candidate_id", "")),
                    candidates_by_id,
                    candidates_by_short,
                )
                if cid:
                    resolved_members.append((cid, member))

            unique_member_ids = {cid for cid, _m in resolved_members}
            if len(unique_member_ids) < 2:
                continue

            group_type = group.get("group_type") or "related"
            if group_type not in {"duplicate", "near_duplicate", "conflict", "related"}:
                group_type = "related"

            try:
                group_confidence = float(group.get("confidence", 0.7))
            except Exception:
                group_confidence = 0.7

            cur.execute(
                """
                INSERT INTO candidate_fact_dedup_groups (
                  dedup_run_id,
                  group_type,
                  canonical_candidate_fact_id,
                  status,
                  group_reason,
                  confidence
                )
                VALUES (%s, %s, %s, 'pending', %s, %s)
                RETURNING id;
                """,
                (
                    dedup_run_id,
                    group_type,
                    canonical_id,
                    normalize_space(group.get("group_reason", "")),
                    group_confidence,
                ),
            )
            group_id = cur.fetchone()[0]
            valid_groups += 1

            for cid, member in resolved_members:
                role = member.get("member_role") or "related"
                if role not in {"canonical", "duplicate", "conflict", "related"}:
                    role = "related"

                action = member.get("suggested_action") or "ask_user"
                if action not in {"keep", "reject_duplicate", "needs_edit", "ask_user"}:
                    action = "ask_user"

                try:
                    member_confidence = float(member.get("confidence", 0.7))
                except Exception:
                    member_confidence = 0.7

                cur.execute(
                    """
                    INSERT INTO candidate_fact_dedup_group_members (
                      group_id,
                      candidate_fact_id,
                      member_role,
                      suggested_action,
                      confidence,
                      reasoning
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (group_id, candidate_fact_id)
                    DO NOTHING;
                    """,
                    (
                        group_id,
                        cid,
                        role,
                        action,
                        member_confidence,
                        normalize_space(member.get("reasoning", "")),
                    ),
                )

        cur.execute(
            """
            UPDATE candidate_fact_dedup_runs
            SET output_group_count = %s
            WHERE id = %s;
            """,
            (valid_groups, dedup_run_id),
        )

    conn.commit()

    print(f"component_run_id: {component_run_id}")
    print(f"dedup_run_id:     {dedup_run_id}")
    print(f"input candidates: {len(candidates)}")
    print(f"raw groups:       {len(groups)}")
    print(f"valid groups:     {valid_groups}")


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) >= 2 else 30

    print("===== SEMANTIC DEDUP WORKER =====")
    print(f"Base URL: {LOCAL_LLM_BASE_URL}")
    print(f"Model:    {LOCAL_LLM_MODEL}")
    print(f"Limit:    {limit}")
    print("")

    with psycopg.connect(DSN, autocommit=False) as conn:
        candidates = fetch_candidates(conn, limit)

        print(f"Candidates selected: {len(candidates)}")
        if len(candidates) < 2:
            print("Need at least 2 candidates to deduplicate.")
            return 0

        for c in candidates:
            print(f"- {c['short_id']} [{c['status']}] {c['fact_text']}")

        print("")
        result = call_ollama(candidates)
        insert_results(conn, candidates, result)

    print("")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
