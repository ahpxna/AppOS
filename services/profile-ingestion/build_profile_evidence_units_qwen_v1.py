import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = get_model("profile_evidence_unit")
VERSION = "profile_evidence_unit_builder_qwen_v1_2026_04_27"


SYSTEM_PROMPT = """You are the Profile Evidence Unit Builder inside a job-application operating system.

Your job is to convert mapped profile document sections into source-grounded evidence units.

Do NOT write resume bullets.
Do NOT create final profile assets.
Do NOT flatten the document into generic skill lists.
Do NOT invent facts, grades, publication venues, awards, employment, certifications, or completed results.
Do NOT treat source papers or guidance as user truth.
Use only the provided document metadata and sections.

Each evidence unit must preserve:
- what the source supports
- what the source does NOT support
- career/role relevance
- tools/frameworks/methods only when grounded in section text
- limits and overclaim boundaries

Return JSON only:
{
  "evidence_units": [
    {
      "section_index": 1,
      "evidence_type": "identity|education|coursework|project_scope|methodology|result|tool_workflow|technical_skill|strategic_analysis|communication|leadership|resume_phrase|career_positioning|limitation|warning",
      "evidence_title": "specific title",
      "direct_quote": "short exact supporting quote from source text, if available",
      "evidence_summary": "source-grounded summary",
      "supports_claims": ["claim this evidence can support"],
      "does_not_support_claims": ["claim this evidence must not be used to support"],
      "role_families": ["cybersecurity_analyst", "network_security", "software_engineering", "data_database", "grc", "ai_research", "dfir", "application_security"],
      "competency_tags": ["specific competency"],
      "tool_tags": ["specific tools only if present"],
      "project_tags": ["project/course/source tag"],
      "source_confidence": 0.80,
      "grounding_confidence": 0.80
    }
  ]
}

Return at most 8 evidence units per document. Prefer fewer, richer, high-signal units over many tiny facts.
"""


def parse_json_content(content: str) -> Dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def call_ollama_json(prompt: str, model: str, retries: int = 2) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.05,
            "num_ctx": 8192,
        },
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("message", {}).get("content", "")
                return parse_json_content(content)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Ollama JSON call failed after {retries} retries: {last_error}")


def clean_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def clamp_float(value: Any, default: float = 0.80) -> float:
    try:
        x = float(value)
        if x < 0:
            return 0.0
        if x > 1:
            return 1.0
        return x
    except Exception:
        return default


def normalize_unit(unit: Dict[str, Any]) -> Dict[str, Any]:
    allowed_types = {
        "identity", "education", "coursework", "project_scope", "methodology", "result",
        "tool_workflow", "technical_skill", "strategic_analysis", "communication",
        "leadership", "resume_phrase", "career_positioning", "limitation", "warning",
    }

    evidence_type = str(unit.get("evidence_type") or "coursework").strip()
    if evidence_type not in allowed_types:
        evidence_type = "coursework"

    return {
        "section_index": int(unit.get("section_index") or 1),
        "evidence_type": evidence_type,
        "evidence_title": str(unit.get("evidence_title") or "Untitled evidence unit").strip()[:300],
        "direct_quote": str(unit.get("direct_quote") or "").strip()[:2000],
        "evidence_summary": str(unit.get("evidence_summary") or "").strip()[:4000],
        "supports_claims": clean_list(unit.get("supports_claims")),
        "does_not_support_claims": clean_list(unit.get("does_not_support_claims")),
        "role_families": clean_list(unit.get("role_families")),
        "competency_tags": clean_list(unit.get("competency_tags")),
        "tool_tags": clean_list(unit.get("tool_tags")),
        "project_tags": clean_list(unit.get("project_tags")),
        "source_confidence": clamp_float(unit.get("source_confidence"), 0.80),
        "grounding_confidence": clamp_float(unit.get("grounding_confidence"), 0.80),
    }


def fetch_docs(cur, limit: int, document_type: Optional[str]):
    if document_type:
        cur.execute(
            """
            SELECT
              pd.id,
              rf.id AS raw_file_id,
              rf.file_name,
              pd.document_title,
              pd.document_type,
              pd.source_role,
              pd.document_summary,
              pd.risk_notes
            FROM v_profile_documents_ready_for_evidence q
            JOIN profile_documents pd ON pd.id = q.profile_document_id
            JOIN raw_files rf ON rf.id = pd.raw_file_id
            WHERE pd.document_type = %s
              AND NOT EXISTS (
                SELECT 1
                FROM profile_evidence_units peu
                WHERE peu.profile_document_id = pd.id
                  AND peu.builder_version = %s
              )
            ORDER BY rf.file_name
            LIMIT %s
            """,
            (document_type, VERSION, limit),
        )
    else:
        cur.execute(
            """
            SELECT
              pd.id,
              rf.id AS raw_file_id,
              rf.file_name,
              pd.document_title,
              pd.document_type,
              pd.source_role,
              pd.document_summary,
              pd.risk_notes
            FROM v_profile_documents_ready_for_evidence q
            JOIN profile_documents pd ON pd.id = q.profile_document_id
            JOIN raw_files rf ON rf.id = pd.raw_file_id
            WHERE NOT EXISTS (
              SELECT 1
              FROM profile_evidence_units peu
              WHERE peu.profile_document_id = pd.id
                AND peu.builder_version = %s
            )
            ORDER BY
              CASE pd.document_type
                WHEN 'official_transcript' THEN 1
                WHEN 'project_profile' THEN 2
                WHEN 'research_profile' THEN 3
                WHEN 'cross_portfolio_mapping' THEN 4
                WHEN 'course_profile' THEN 5
                ELSE 9
              END,
              rf.file_name
            LIMIT %s
            """,
            (VERSION, limit),
        )

    return cur.fetchall()


def fetch_sections(cur, document_id, max_sections: int, max_chars_per_section: int):
    cur.execute(
        """
        SELECT
          pds.id,
          pds.chunk_id,
          pds.raw_file_id,
          pds.section_index,
          pds.section_title,
          pds.section_type,
          pds.section_text
        FROM profile_document_sections pds
        WHERE pds.profile_document_id = %s
        ORDER BY pds.section_index
        LIMIT %s
        """,
        (document_id, max_sections),
    )

    rows = []
    section_by_index = {}

    for section_id, chunk_id, raw_file_id, idx, title, section_type, text in cur.fetchall():
        t = text or ""
        if len(t) > max_chars_per_section:
            t = t[:max_chars_per_section] + "\n[TRUNCATED_FOR_EVIDENCE_BUILDER]"

        row = {
            "section_id": section_id,
            "chunk_id": chunk_id,
            "raw_file_id": raw_file_id,
            "section_index": idx,
            "section_title": title,
            "section_type": section_type,
            "section_text": t,
        }
        rows.append(row)
        section_by_index[int(idx)] = row

    return rows, section_by_index


def build_prompt(doc_row, sections: List[Dict[str, Any]]) -> str:
    (
        doc_id,
        raw_file_id,
        file_name,
        document_title,
        document_type,
        source_role,
        document_summary,
        risk_notes,
    ) = doc_row

    payload = {
        "document_metadata": {
            "file_name": file_name,
            "document_title": document_title,
            "document_type": document_type,
            "source_role": source_role,
            "document_summary": document_summary,
            "risk_notes": risk_notes or [],
        },
        "sections": [
            {
                "section_index": s["section_index"],
                "section_title": s["section_title"],
                "section_type": s["section_type"],
                "section_text": s["section_text"],
            }
            for s in sections
        ],
    }

    return (
        "Build high-signal profile evidence units from this mapped document. "
        "Use only this source. Preserve evidence boundaries. JSON only.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def insert_evidence_unit(cur, doc_row, section_by_index, unit: Dict[str, Any], builder_model: str):
    (
        doc_id,
        fallback_raw_file_id,
        file_name,
        document_title,
        document_type,
        source_role,
        document_summary,
        risk_notes,
    ) = doc_row

    section = section_by_index.get(unit["section_index"])
    section_id = section["section_id"] if section else None
    chunk_id = section["chunk_id"] if section else None
    raw_file_id = section["raw_file_id"] if section else fallback_raw_file_id

    cur.execute(
        """
        INSERT INTO profile_evidence_units (
          profile_document_id,
          profile_document_section_id,
          raw_file_id,
          chunk_id,
          evidence_type,
          evidence_title,
          direct_quote,
          evidence_summary,
          supports_claims,
          does_not_support_claims,
          role_families,
          competency_tags,
          tool_tags,
          project_tags,
          abstraction_level,
          source_confidence,
          grounding_confidence,
          status,
          builder_version,
          builder_model
        )
        VALUES (
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s,
          %s, %s, %s, %s,
          'evidence_unit',
          %s, %s,
          'draft',
          %s, %s
        )
        """,
        (
            doc_id,
            section_id,
            raw_file_id,
            chunk_id,
            unit["evidence_type"],
            unit["evidence_title"],
            unit["direct_quote"],
            unit["evidence_summary"],
            unit["supports_claims"],
            unit["does_not_support_claims"],
            unit["role_families"],
            unit["competency_tags"],
            unit["tool_tags"],
            unit["project_tags"],
            unit["source_confidence"],
            unit["grounding_confidence"],
            VERSION,
            builder_model,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--document-type", default=None)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-sections", type=int, default=16)
    parser.add_argument("--max-chars-per-section", type=int, default=1800)
    args = parser.parse_args()

    # args.model is passed explicitly; module MODEL remains the default.

    print("===== PROFILE EVIDENCE UNIT BUILDER QWEN V1 =====")
    print(f"Version:       {VERSION}")
    print(f"Mode:          {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Model:         {args.model}")
    print(f"Limit:         {args.limit}")
    print(f"Document type: {args.document_type}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            docs = fetch_docs(cur, args.limit, args.document_type)

            print(f"Ready documents selected: {len(docs)}")

            inserted = 0
            failed = 0

            for i, doc in enumerate(docs, start=1):
                doc_id, raw_file_id, file_name, title, doc_type, source_role, *_ = doc
                sections, section_by_index = fetch_sections(
                    cur,
                    doc_id,
                    args.max_sections,
                    args.max_chars_per_section,
                )

                print("")
                print(f"--- Document {i}/{len(docs)} ---")
                print(f"File:     {file_name}")
                print(f"Title:    {title}")
                print(f"Type:     {doc_type}")
                print(f"Role:     {source_role}")
                print(f"Sections: {len(sections)}")

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT evidence_builder_sp")
                try:
                    prompt = build_prompt(doc, sections)
                    result = call_ollama_json(prompt, args.model)
                    units_raw = result.get("evidence_units", [])

                    if not isinstance(units_raw, list):
                        raise RuntimeError("Model output does not contain evidence_units list.")

                    # Idempotent for this builder version.
                    cur.execute(
                        """
                        DELETE FROM profile_evidence_units
                        WHERE profile_document_id = %s
                          AND builder_version = %s
                          AND status = 'draft'
                        """,
                        (doc_id, VERSION),
                    )

                    doc_inserted = 0
                    for raw_unit in units_raw[:8]:
                        unit = normalize_unit(raw_unit)
                        if not unit["evidence_summary"]:
                            continue
                        insert_evidence_unit(cur, doc, section_by_index, unit, args.model)
                        doc_inserted += 1

                    cur.execute("RELEASE SAVEPOINT evidence_builder_sp")
                    inserted += doc_inserted

                    print(f"Evidence units inserted: {doc_inserted}")
                    conn.commit()

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT evidence_builder_sp")
                    cur.execute("RELEASE SAVEPOINT evidence_builder_sp")
                    failed += 1
                    print(f"FAILED: {e}")

            if args.apply:
                conn.commit()

    print("")
    print("===== SUMMARY =====")
    print(f"Documents selected: {len(docs)}")
    print(f"Evidence inserted:  {inserted}")
    print(f"Failed documents:   {failed}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
