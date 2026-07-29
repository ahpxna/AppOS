import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import psycopg
import requests


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "llama3.2:3b")

EXTRACTOR_NAME = "local_ollama_candidate_fact_extractor"
EXTRACTOR_VERSION = "ollama_structured_v2_require_evidence_2026_04_26"

DEFAULT_LIMIT = int(os.getenv("JOBOS_FACT_EXTRACT_LIMIT", "3"))
MIN_TOKEN_COUNT = int(os.getenv("JOBOS_FACT_MIN_TOKENS", "40"))
SLEEP_SECONDS = float(os.getenv("JOBOS_FACT_SLEEP_SECONDS", "0.3"))

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)


FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "academic",
                            "skills",
                            "projects",
                            "experience",
                            "research",
                            "leadership",
                            "certifications",
                            "awards",
                            "career_positioning",
                            "other",
                        ],
                    },
                    "subcategory": {"type": "string"},
                    "fact_text": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "category",
                    "subcategory",
                    "fact_text",
                    "evidence_quote",
                    "confidence",
                    "reasoning",
                ],
            },
        }
    },
    "required": ["facts"],
}


SYSTEM_PROMPT = """You are an evidence-grounded profile fact extraction engine for a job application automation system.

Return JSON only.

Task:
Extract concise, truthful candidate facts from the provided profile chunk.

Hard rules:
- Extract only facts explicitly supported by the chunk.
- Every fact MUST include a non-empty evidence_quote copied from the chunk.
- If you cannot copy a supporting quote, do not extract that fact.
- Do not infer beyond the evidence.
- Do not transform "in progress", "IP", "planned", "optional", "roadmap", "to prioritize next", or "future" into completed current experience.
- Do not extract contact details, phone numbers, home addresses, emails, LinkedIn URLs, GitHub URLs, or portfolio URLs.
- Do not infer sensitive identity attributes: race, ethnicity, gender, disability, veteran status, religion, politics, health, citizenship, visa status.
- A course title about race/gender/society may be extracted as coursework only, never as the user's identity.
- Do not claim certifications unless the chunk explicitly says they are earned or completed.
- Prefer facts useful for resumes, cover letters, interviews, job matching, or application forms.
- If no useful evidence-grounded facts exist, return {"facts": []}.

Fact style:
- Facts must be atomic, not broad resume-summary paragraphs.
- Bad: "User is a strong fit for entry-level roles."
- Good: "User is a Bachelor of Science senior at Rider University."
- Good: "User has coursework in Computer Networks and Operating Systems and Cybersecurity."

Quality:
- Max 4 facts per chunk.
- fact_text should be one sentence.
- fact_text should usually start with "User..."
- confidence should usually be between 0.70 and 0.95.
- Use category carefully: degree/coursework = academic, tools/programming = skills, portfolio work = projects, jobs/tutoring/team lead = experience, research work = research, profile summary = career_positioning.
"""


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_ollama_message_content(response_json: Dict[str, Any]) -> Dict[str, Any]:
    content = response_json.get("message", {}).get("content", "")
    if not content:
        raise ValueError(f"No message.content in Ollama response: {response_json}")

    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"```$", "", content).strip()

    return json.loads(content)


def call_local_ollama(chunk: Dict[str, Any]) -> Dict[str, Any]:
    prompt_payload = {
        "source_file": chunk["file_name"],
        "chunk_index": chunk["chunk_index"],
        "section": chunk["section"],
        "chunk_category_hint": chunk["category"],
        "schema": FACT_SCHEMA,
        "text": chunk["text_content"],
    }

    body = {
        "model": LOCAL_LLM_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract candidate profile facts from this chunk. "
                    "Return JSON exactly matching the schema. "
                    + json.dumps(prompt_payload, ensure_ascii=False)
                ),
            },
        ],
        "format": FACT_SCHEMA,
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
    parsed["_raw_ollama"] = {
        "model": data.get("model"),
        "total_duration": data.get("total_duration"),
        "load_duration": data.get("load_duration"),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
    }
    return parsed


def get_chunks(conn, limit: int) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              pc.id,
              pc.file_id,
              rf.file_name,
              pc.chunk_index,
              pc.section,
              pc.category,
              pc.token_count,
              pc.text_content
            FROM profile_chunks pc
            JOIN raw_files rf
              ON rf.id = pc.file_id
            WHERE
              rf.source = 'local_profile_ingestion'
              AND rf.parse_status = 'parsed'
              AND pc.token_count >= %s
              AND NOT EXISTS (
                SELECT 1
                FROM candidate_profile_facts cpf
                WHERE cpf.source_chunk_id = pc.id
                  AND cpf.extractor_name = %s
                  AND cpf.extractor_version = %s
              )
            ORDER BY
              rf.uploaded_at DESC,
              pc.chunk_index ASC
            LIMIT %s;
            """,
            (MIN_TOKEN_COUNT, EXTRACTOR_NAME, EXTRACTOR_VERSION, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "id": str(row[0]),
            "file_id": str(row[1]),
            "file_name": row[2],
            "chunk_index": row[3],
            "section": row[4],
            "category": row[5],
            "token_count": row[6],
            "text_content": row[7],
        }
        for row in rows
    ]


def clean_fact(candidate: Dict[str, Any], chunk_text: str) -> Optional[Dict[str, Any]]:
    fact_text = normalize_space(str(candidate.get("fact_text", "")))
    evidence_quote = normalize_space(str(candidate.get("evidence_quote", "")))

    if not fact_text or len(fact_text) < 20:
        return None

    # Evidence is mandatory. Candidate facts without evidence are not usable.
    if not evidence_quote or len(evidence_quote) < 8:
        return None

    category = normalize_space(str(candidate.get("category", "other"))).lower()
    subcategory = normalize_space(str(candidate.get("subcategory", "general"))).lower()
    reasoning = normalize_space(str(candidate.get("reasoning", "")))

    try:
        confidence = float(candidate.get("confidence", 0.70))
    except Exception:
        confidence = 0.70

    confidence = max(0.0, min(confidence, 0.99))

    compact_chunk = normalize_space(chunk_text).lower()
    compact_quote = normalize_space(evidence_quote).lower()

    # If quote cannot be found even approximately, skip the fact.
    # This keeps profile_facts evidence-grounded.
    if compact_quote not in compact_chunk:
        return None

    allowed = {
        "academic",
        "skills",
        "projects",
        "experience",
        "research",
        "leadership",
        "certifications",
        "awards",
        "career_positioning",
        "other",
    }
    if category not in allowed:
        category = "other"

    # Extra local guardrail for obvious roadmap/certification hallucinations.
    bad_current_cert_words = ["security+", "cysa+", "cissp", "sscp", "az-500", "sc-900"]
    lower_fact = fact_text.lower()
    lower_chunk = chunk_text.lower()
    if any(w in lower_fact for w in bad_current_cert_words):
        if any(x in lower_chunk for x in ["roadmap", "optional", "to prioritize next", "highest roi"]):
            confidence = min(confidence, 0.45)
            reasoning = (reasoning + " Certification appears in roadmap/future context; review carefully.").strip()

    return {
        "category": category,
        "subcategory": subcategory or "general",
        "fact_text": fact_text,
        "evidence_quote": evidence_quote,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def insert_candidates(conn, chunk: Dict[str, Any], facts: List[Dict[str, Any]]) -> int:
    inserted = 0

    with conn.cursor() as cur:
        for fact in facts:
            cleaned = clean_fact(fact, chunk["text_content"])
            if not cleaned:
                continue

            dedup_key = stable_hash(
                "|".join(
                    [
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                        chunk["id"],
                        cleaned["category"],
                        cleaned["subcategory"],
                        normalize_space(cleaned["fact_text"]).lower(),
                    ]
                )
            )

            cur.execute(
                """
                INSERT INTO candidate_profile_facts (
                  source_file_id,
                  source_chunk_id,
                  extractor_name,
                  extractor_version,
                  category,
                  subcategory,
                  fact_text,
                  evidence_quote,
                  reasoning,
                  confidence,
                  status,
                  dedup_key
                )
                VALUES (
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s,
                  'pending',
                  %s
                )
                ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL
                DO NOTHING
                RETURNING id;
                """,
                (
                    chunk["file_id"],
                    chunk["id"],
                    EXTRACTOR_NAME,
                    EXTRACTOR_VERSION,
                    cleaned["category"],
                    cleaned["subcategory"],
                    cleaned["fact_text"],
                    cleaned["evidence_quote"],
                    cleaned["reasoning"],
                    cleaned["confidence"],
                    dedup_key,
                ),
            )

            if cur.fetchone():
                inserted += 1

    return inserted


def log_cost_like_metrics(conn, ollama_meta: Optional[Dict[str, Any]]) -> None:
    if not ollama_meta:
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cost_ledger (
              agent_name,
              model_name,
              input_tokens,
              output_tokens,
              estimated_cost_usd
            )
            VALUES (%s, %s, %s, %s, 0);
            """,
            (
                EXTRACTOR_NAME,
                ollama_meta.get("model") or LOCAL_LLM_MODEL,
                ollama_meta.get("prompt_eval_count"),
                ollama_meta.get("eval_count"),
            ),
        )


def main() -> int:
    limit = DEFAULT_LIMIT
    if len(sys.argv) >= 2:
        limit = int(sys.argv[1])

    print("===== LOCAL LLM CANDIDATE FACT EXTRACTOR =====")
    print(f"Base URL:          {LOCAL_LLM_BASE_URL}")
    print(f"Model:             {LOCAL_LLM_MODEL}")
    print(f"Extractor:         {EXTRACTOR_NAME}")
    print(f"Version:           {EXTRACTOR_VERSION}")
    print(f"Chunk limit:       {limit}")
    print("")

    total_inserted = 0

    with psycopg.connect(DSN, autocommit=False) as conn:
        chunks = get_chunks(conn, limit)
        print(f"Chunks selected:   {len(chunks)}")

        if not chunks:
            print("No eligible chunks found.")
            return 0

        for i, chunk in enumerate(chunks, start=1):
            print("")
            print(f"--- Chunk {i}/{len(chunks)} ---")
            print(f"File:     {chunk['file_name']}")
            print(f"Index:    {chunk['chunk_index']}")
            print(f"Section:  {chunk['section']}")
            print(f"Category: {chunk['category']}")
            print(f"Tokens:   {chunk['token_count']}")

            try:
                result = call_local_ollama(chunk)
                facts = result.get("facts", [])
                if not isinstance(facts, list):
                    raise ValueError("LLM JSON field 'facts' is not a list")

                inserted = insert_candidates(conn, chunk, facts)
                log_cost_like_metrics(conn, result.get("_raw_ollama"))
                conn.commit()

                total_inserted += inserted
                print(f"LLM facts returned: {len(facts)}")
                print(f"Inserted:           {inserted}")

            except Exception as e:
                conn.rollback()
                print(f"ERROR on chunk {chunk['id']}: {e}")

            time.sleep(SLEEP_SECONDS)

    print("")
    print("===== DONE =====")
    print(f"Total inserted: {total_inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
