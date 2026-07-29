import argparse
import json
import os
import re
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb


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
MODEL = os.getenv("PROFILE_DOCUMENT_MAPPER_MODEL", "qwen3:8b")
VERSION = "profile_document_mapper_qwen_v1_source_preserving_2026_04_27"


SYSTEM_PROMPT = """You are the Profile Document Mapper inside a job-application operating system.

Your task is NOT to create resume bullets and NOT to extract tiny facts.
Your task is to map one source document into a structured understanding layer.

You must preserve source boundaries and avoid overclaiming.

Important rules:
1. Distinguish user-owned evidence from background/reference material.
2. Do not turn source papers/course readings into claims about the user.
3. Do not treat guidance/planning documents as completed credentials.
4. Do not treat research proposals as completed empirical results.
5. Do not flatten rich documents into generic skill lists.
6. Preserve project scope, methodology, tools, results, limitations, career relevance, and do-not-overclaim boundaries.
7. Do not invent publication venue, publication year, advisor status, grade, completion status, empirical validation, or external recognition unless explicitly present in the provided sections.
8. If the source is a paper/project file, describe it as "document", "project paper", "project profile", or "research proposal" based only on metadata and provided text.
9. Risk notes must be specific and non-duplicative. Do not repeat the same warning with different wording.
10. Output JSON only. No markdown. No explanation outside JSON.

Return this JSON shape:
{
  "document_summary": "dense but readable summary of what this document is and why it matters for the profile; do not mention publication venue/year unless explicitly provided",
  "document_purpose": "what this document should be used for inside the profile system",
  "recommended_processing_strategy": "how downstream evidence/unit/asset builders should use it",
  "source_risk_level": "low|medium|high",
  "risk_notes": ["risk or boundary note"],
  "section_map": [
    {
      "section_index": 1,
      "section_title": "title",
      "semantic_type": "scope|methodology|result|tool_workflow|career_positioning|limitation|academic_record|reference_background|guidance|source_section",
      "importance": "low|medium|high",
      "summary": "what this section contributes",
      "supports_profile_assets": true,
      "do_not_use_as_direct_claim": false
    }
  ],
  "candidate_asset_directions": [
    {
      "asset_direction": "possible future profile asset title or angle",
      "asset_type": "project_asset|course_competency_asset|research_asset|tool_workflow_asset|academic_trajectory_asset|reference_only|guidance_only",
      "why_it_matters": "career/profile reason",
      "evidence_boundary": "what this document supports and does not support"
    }
  ],
  "do_not_overclaim_rules": ["specific rule"],
  "role_relevance": ["cybersecurity_analyst", "network_security", "software_engineering", "data_database", "grc", "ai_research", "dfir", "application_security"]
}
"""


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


def parse_json_content(content: str) -> Dict[str, Any]:
    text = content.strip()

    # Remove markdown fences if model disobeys.
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: extract first JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError(f"Could not parse JSON from model output: {content[:500]}")


def fetch_smoke_docs(cur):
    # Real smoke batch: one official transcript, one project/research profile, one cross mapping.
    cur.execute(
        """
        WITH ranked AS (
          SELECT
            pd.id,
            pd.document_type,
            row_number() OVER (
              PARTITION BY
                CASE
                  WHEN pd.document_type = 'official_transcript' THEN 'official'
                  WHEN pd.document_type IN ('project_profile', 'research_profile') THEN 'project'
                  WHEN pd.document_type = 'cross_portfolio_mapping' THEN 'mapping'
                  ELSE pd.document_type
                END
              ORDER BY pd.created_at ASC
            ) AS rn,
            CASE
              WHEN pd.document_type = 'official_transcript' THEN 1
              WHEN pd.document_type IN ('project_profile', 'research_profile') THEN 2
              WHEN pd.document_type = 'cross_portfolio_mapping' THEN 3
              ELSE 9
            END AS bucket_order
          FROM profile_documents pd
          WHERE pd.status IN ('needs_mapping', 'mapped')
            AND pd.document_type IN ('official_transcript', 'project_profile', 'research_profile', 'cross_portfolio_mapping')
        )
        SELECT id
        FROM ranked
        WHERE rn = 1
          AND bucket_order < 9
        ORDER BY bucket_order
        LIMIT 3;
        """
    )
    return [r[0] for r in cur.fetchall()]


def fetch_docs(cur, limit: int, smoke: bool, document_type: Optional[str]):
    if smoke:
        ids = fetch_smoke_docs(cur)
        if not ids:
            return []
        cur.execute(
            """
            SELECT
              pd.id,
              rf.file_name,
              pd.document_title,
              pd.document_type,
              pd.source_role,
              pd.document_purpose,
              pd.contains_profile_evidence,
              pd.contains_guidance_only
            FROM profile_documents pd
            LEFT JOIN raw_files rf ON rf.id = pd.raw_file_id
            WHERE pd.id = ANY(%s)
            ORDER BY
              CASE
                WHEN pd.document_type = 'official_transcript' THEN 1
                WHEN pd.document_type IN ('project_profile', 'research_profile') THEN 2
                WHEN pd.document_type = 'cross_portfolio_mapping' THEN 3
                ELSE 9
              END;
            """,
            (ids,),
        )
        return cur.fetchall()

    if document_type:
        cur.execute(
            """
            SELECT
              pd.id,
              rf.file_name,
              pd.document_title,
              pd.document_type,
              pd.source_role,
              pd.document_purpose,
              pd.contains_profile_evidence,
              pd.contains_guidance_only
            FROM profile_documents pd
            LEFT JOIN raw_files rf ON rf.id = pd.raw_file_id
            WHERE pd.status = 'needs_mapping'
              AND pd.document_type = %s
            ORDER BY pd.created_at ASC
            LIMIT %s;
            """,
            (document_type, limit),
        )
    else:
        cur.execute(
            """
            SELECT
              pd.id,
              rf.file_name,
              pd.document_title,
              pd.document_type,
              pd.source_role,
              pd.document_purpose,
              pd.contains_profile_evidence,
              pd.contains_guidance_only
            FROM profile_documents pd
            LEFT JOIN raw_files rf ON rf.id = pd.raw_file_id
            WHERE pd.status = 'needs_mapping'
            ORDER BY
              CASE pd.document_type
                WHEN 'official_transcript' THEN 1
                WHEN 'project_profile' THEN 2
                WHEN 'research_profile' THEN 3
                WHEN 'cross_portfolio_mapping' THEN 4
                WHEN 'course_profile' THEN 5
                WHEN 'source_paper' THEN 6
                WHEN 'guidance_not_truth' THEN 7
                ELSE 9
              END,
              pd.created_at ASC
            LIMIT %s;
            """,
            (limit,),
        )
    return cur.fetchall()


def fetch_sections(cur, document_id, max_sections: int, max_chars_per_section: int) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
          section_index,
          section_title,
          section_type,
          importance_score,
          section_text
        FROM profile_document_sections
        WHERE profile_document_id = %s
        ORDER BY section_index ASC
        LIMIT %s;
        """,
        (document_id, max_sections),
    )

    out = []
    for idx, title, section_type, importance, text in cur.fetchall():
        t = text or ""
        if len(t) > max_chars_per_section:
            t = t[:max_chars_per_section] + "\n[TRUNCATED_FOR_MAPPING]"
        out.append(
            {
                "section_index": idx,
                "section_title": title,
                "current_section_type": section_type,
                "importance_score": float(importance) if importance is not None else None,
                "text": t,
            }
        )
    return out


def build_prompt(doc_row, sections: List[Dict[str, Any]]) -> str:
    (
        doc_id,
        file_name,
        document_title,
        document_type,
        source_role,
        document_purpose,
        contains_profile_evidence,
        contains_guidance_only,
    ) = doc_row

    payload = {
        "document_metadata": {
            "file_name": file_name,
            "document_title": document_title,
            "document_type": document_type,
            "source_role": source_role,
            "document_purpose": document_purpose,
            "contains_profile_evidence": contains_profile_evidence,
            "contains_guidance_only": contains_guidance_only,
        },
        "sections": sections,
    }

    return (
        "Return valid JSON only. Start with { and end with }. "
        "Map this profile source document. Preserve evidence boundaries. "
        "Do not create final resume claims yet.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def normalize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    result.setdefault("document_summary", "")
    result.setdefault("document_purpose", "")
    result.setdefault("recommended_processing_strategy", "")
    result.setdefault("source_risk_level", "medium")
    result.setdefault("risk_notes", [])
    result.setdefault("section_map", [])
    result.setdefault("candidate_asset_directions", [])
    result.setdefault("do_not_overclaim_rules", [])
    result.setdefault("role_relevance", [])

    # Ensure arrays are arrays.
    for k in ["risk_notes", "section_map", "candidate_asset_directions", "do_not_overclaim_rules", "role_relevance"]:
        if not isinstance(result.get(k), list):
            result[k] = [str(result.get(k))]

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--smoke", action="store_true", help="Map one official, one project/research, one mapping document.")
    parser.add_argument("--document-type", default=None)
    parser.add_argument("--max-sections", type=int, default=24)
    parser.add_argument("--max-chars-per-section", type=int, default=2200)
    args = parser.parse_args()

    print("===== PROFILE DOCUMENT MAPPER QWEN V1 =====")
    print(f"Version:       {VERSION}")
    print(f"Mode:          {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Model:         {args.model}")
    print(f"Limit:         {args.limit}")
    print(f"Smoke:         {args.smoke}")
    print(f"Document type: {args.document_type}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            docs = fetch_docs(cur, args.limit, args.smoke, args.document_type)

            print(f"Documents selected: {len(docs)}")

            mapped = 0
            failed = 0

            for i, doc in enumerate(docs, start=1):
                doc_id, file_name, title, doc_type, source_role, *_ = doc
                sections = fetch_sections(cur, doc_id, args.max_sections, args.max_chars_per_section)

                print("")
                print(f"--- Document {i}/{len(docs)} ---")
                print(f"File:      {file_name}")
                print(f"Title:     {title}")
                print(f"Type:      {doc_type}")
                print(f"Role:      {source_role}")
                print(f"Sections:  {len(sections)}")

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT document_map_sp")
                try:
                    prompt = build_prompt(doc, sections)
                    result = normalize_result(call_ollama_json(prompt, args.model))

                    structure = {
                        "mapper_version": VERSION,
                        "mapper_model": args.model,
                        "source_risk_level": result.get("source_risk_level"),
                        "recommended_processing_strategy": result.get("recommended_processing_strategy"),
                        "section_map": result.get("section_map", []),
                        "candidate_asset_directions": result.get("candidate_asset_directions", []),
                        "do_not_overclaim_rules": result.get("do_not_overclaim_rules", []),
                        "role_relevance": result.get("role_relevance", []),
                    }

                    risk_notes = result.get("risk_notes", [])
                    do_not = result.get("do_not_overclaim_rules", [])
                    merged_risk_notes = [str(x) for x in (risk_notes + do_not) if str(x).strip()]

                    cur.execute(
                        """
                        UPDATE profile_documents
                        SET
                          document_summary = %s,
                          document_purpose = COALESCE(NULLIF(%s, ''), document_purpose),
                          structure_json = %s,
                          risk_notes = %s,
                          mapper_version = %s,
                          mapper_model = %s,
                          status = 'mapped',
                          updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            result.get("document_summary", ""),
                            result.get("document_purpose", ""),
                            Jsonb(structure),
                            merged_risk_notes,
                            VERSION,
                            args.model,
                            doc_id,
                        ),
                    )

                    cur.execute("RELEASE SAVEPOINT document_map_sp")
                    mapped += 1

                    print("Mapped.")
                    print("Summary preview:")
                    print((result.get("document_summary") or "")[:500])

                    conn.commit()

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT document_map_sp")
                    cur.execute("RELEASE SAVEPOINT document_map_sp")
                    failed += 1
                    print(f"FAILED: {e}")

            if args.apply:
                conn.commit()

    print("")
    print("===== SUMMARY =====")
    print(f"Selected: {len(docs)}")
    print(f"Mapped:   {mapped}")
    print(f"Failed:   {failed}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
