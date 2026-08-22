import argparse
import json
import os
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional

import psycopg

# PATCH: module an toan dung chung (evidence strength + overclaim rules).
import sys as _sys
from pathlib import Path as _Path

_COMMON = _Path(__file__).resolve().parents[1] / "common"
if str(_COMMON.parent) not in _sys.path:
    _sys.path.insert(0, str(_COMMON.parent))
from common import jobos_safety as _safety  # noqa: E402
from common.llm_gateway import chat_text as _chat_text  # noqa: E402
from common import model_config as _model_config  # noqa: E402



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
DEFAULT_MODEL = _model_config.get_model("profile_asset_synthesizer")
VERSION = "profile_asset_synthesizer_qwen_v1_2026_04_27"


SYSTEM_PROMPT = """You are the Profile Asset Synthesizer inside a job-application operating system.

Your job is to synthesize rich, job-oriented profile assets from source-grounded evidence units.

Do NOT write final resume output.
Do NOT approve the asset.
Do NOT invent claims, employment, certifications, publication venues, grades, awards, completed empirical results, or production deployment.
Do NOT flatten the evidence into "User has skill X".
Preserve methodology, scope, tools, limitations, role relevance, and do-not-overclaim boundaries.

Each asset should be a coherent career/profile asset, not a tiny fact.

Return JSON only:
{
  "assets": [
    {
      "asset_title": "specific career asset title",
      "asset_type": "project_asset|course_competency_asset|research_asset|tool_workflow_asset|academic_record_asset|academic_trajectory_asset|strategic_asset",
      "canonical_narrative": "rich source-preserving narrative",
      "job_oriented_summary": "how this asset should be used for job applications",
      "resume_bullet_bank": ["safe draft bullet or phrase", "another safe draft bullet or phrase"],
      "interview_story": "STAR-style interview story angle, bounded by evidence",
      "cover_letter_positioning": "how to position this asset in a cover letter",
      "role_families": ["cybersecurity_analyst", "network_security", "software_engineering", "data_database", "grc", "ai_research", "dfir", "application_security"],
      "competency_tags": ["specific competency"],
      "tool_tags": ["tools only if grounded"],
      "project_tags": ["course/project/source tags"],
      "do_not_overclaim_rules": ["specific boundary"],
      "confidence": 0.80,
      "evidence_links": [
        {
          "evidence_unit_index": 1,
          "evidence_rank": 1,
          "evidence_role": "primary|supporting|limitation|warning"
        }
      ]
    }
  ]
}

Return 1-2 assets per document. Prefer one strong asset unless the evidence clearly supports two distinct assets.
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
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            content = _chat_text(
                role="profile_asset_synthesizer",
                model=model,
                local_url=OLLAMA_URL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                timeout=600,
                temperature=0.05,
                num_ctx=8192,
                json_mode=True,
            )
            return parse_json_content(content)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"LLM JSON call failed after {retries} retries: {last_error}")


def clean_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def text_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(f"- {str(x).strip()}" for x in value if str(x).strip())
    return str(value).strip()


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


def normalize_asset(asset: Dict[str, Any], fallback_type: str) -> Dict[str, Any]:
    # Hard rule: asset_type comes from document_type, not from the model.
    # Example: project_profile -> project_asset, course_profile -> course_competency_asset.
    asset_type = fallback_type

    return {
        "asset_title": str(asset.get("asset_title") or "Untitled profile asset").strip()[:300],
        "asset_type": asset_type,
        "canonical_narrative": text_field(asset.get("canonical_narrative"))[:8000],
        "job_oriented_summary": text_field(asset.get("job_oriented_summary"))[:4000],
        "resume_bullet_bank": text_field(asset.get("resume_bullet_bank"))[:5000],
        "interview_story": text_field(asset.get("interview_story"))[:5000],
        "cover_letter_positioning": text_field(asset.get("cover_letter_positioning"))[:4000],
        "role_families": clean_list(asset.get("role_families")),
        "competency_tags": clean_list(asset.get("competency_tags")),
        "tool_tags": clean_list(asset.get("tool_tags")),
        "project_tags": clean_list(asset.get("project_tags")),
        "do_not_overclaim_rules": _safety.build_overclaim_rules(asset.get("do_not_overclaim_rules")),
        "confidence": clamp_float(asset.get("confidence"), 0.80),
        "evidence_links": asset.get("evidence_links") if isinstance(asset.get("evidence_links"), list) else [],
    }


def validate_asset_basic(asset: Dict[str, Any], doc_row, evidence_units: List[Dict[str, Any]]) -> tuple[bool, str]:
    (
        doc_id,
        raw_file_id,
        file_name,
        storage_url,
        document_title,
        document_type,
        source_role,
        document_summary,
        risk_notes,
    ) = doc_row

    if not evidence_units:
        return False, "asset_has_no_evidence_units"

    expected_type = fallback_asset_type(document_type)
    if asset.get("asset_type") != expected_type:
        return False, f"wrong_asset_type:{asset.get('asset_type')} expected:{expected_type}"

    narrative = asset.get("canonical_narrative") or ""
    title = asset.get("asset_title") or ""
    all_text = " ".join([
        title,
        narrative,
        asset.get("job_oriented_summary") or "",
        asset.get("resume_bullet_bank") or "",
        asset.get("interview_story") or "",
        asset.get("cover_letter_positioning") or "",
        " ".join(asset.get("do_not_overclaim_rules") or []),
    ]).lower()

    if len(narrative.strip()) < 120:
        return False, "canonical_narrative_too_short"

    forbidden_terms = [
        "neurips",
        "peer-reviewed",
        "published paper",
        "conference paper",
        "journal article",
        "award-winning",
        "certified ",
        "certification earned",
        "professional experience",
        "production deployment",
        "production-ready",
        "enterprise-grade",
        "employed as",
        "worked professionally as",
    ]

    for term in forbidden_terms:
        if term in all_text:
            return False, f"forbidden_external_or_overclaim_term:{term}"

    return True, "ok"


def fallback_asset_type(document_type: str) -> str:
    mapping = {
        "project_profile": "project_asset",
        "research_profile": "research_asset",
        "cross_portfolio_mapping": "tool_workflow_asset",
        "official_transcript": "academic_record_asset",
        "course_profile": "course_competency_asset",
    }
    return mapping.get(document_type, "strategic_asset")


def fetch_docs(cur, limit: int, document_type: Optional[str], force: bool):
    params = []
    where = [
        """EXISTS (
          SELECT 1
          FROM profile_evidence_units peu
          WHERE peu.profile_document_id = pd.id
            AND peu.builder_version = 'profile_evidence_unit_builder_qwen_v1_2026_04_27'
        )"""
    ]

    if document_type:
        where.append("pd.document_type = %s")
        params.append(document_type)

    if not force:
        where.append(
            """NOT EXISTS (
              SELECT 1
              FROM profile_assets pa
              WHERE pa.created_from_raw_file_id = rf.id
                AND pa.compiler_version = %s
            )"""
        )
        params.append(VERSION)

    params.append(limit)

    cur.execute(
        f"""
        SELECT
          pd.id,
          rf.id AS raw_file_id,
          rf.file_name,
          rf.storage_url,
          pd.document_title,
          pd.document_type,
          pd.source_role,
          pd.document_summary,
          pd.risk_notes
        FROM profile_documents pd
        JOIN raw_files rf ON rf.id = pd.raw_file_id
        JOIN v_profile_documents_ready_for_evidence q
          ON q.profile_document_id = pd.id
        WHERE {' AND '.join(where)}
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
        params,
    )
    return cur.fetchall()


def fetch_evidence_units(cur, document_id, max_units: int):
    cur.execute(
        """
        SELECT
          peu.id,
          peu.raw_file_id,
          peu.chunk_id,
          rf.file_name,
          rf.storage_url,
          pds.section_title,
          pds.section_index,
          peu.evidence_type,
          peu.evidence_title,
          peu.direct_quote,
          peu.evidence_summary,
          peu.supports_claims,
          peu.does_not_support_claims,
          peu.role_families,
          peu.competency_tags,
          peu.tool_tags,
          peu.project_tags,
          peu.source_confidence,
          peu.grounding_confidence
        FROM profile_evidence_units peu
        LEFT JOIN raw_files rf ON rf.id = peu.raw_file_id
        LEFT JOIN profile_document_sections pds ON pds.id = peu.profile_document_section_id
        WHERE peu.profile_document_id = %s
          AND peu.builder_version = 'profile_evidence_unit_builder_qwen_v1_2026_04_27'
          AND peu.status = 'draft'
        ORDER BY
          CASE peu.evidence_type
            WHEN 'project_scope' THEN 1
            WHEN 'methodology' THEN 2
            WHEN 'result' THEN 3
            WHEN 'tool_workflow' THEN 4
            WHEN 'technical_skill' THEN 5
            WHEN 'career_positioning' THEN 6
            WHEN 'limitation' THEN 7
            WHEN 'warning' THEN 8
            ELSE 9
          END,
          peu.created_at ASC
        LIMIT %s
        """,
        (document_id, max_units),
    )

    keys = [
        "evidence_unit_id",
        "raw_file_id",
        "chunk_id",
        "file_name",
        "storage_url",
        "section_title",
        "section_index",
        "evidence_type",
        "evidence_title",
        "direct_quote",
        "evidence_summary",
        "supports_claims",
        "does_not_support_claims",
        "role_families",
        "competency_tags",
        "tool_tags",
        "project_tags",
        "source_confidence",
        "grounding_confidence",
    ]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def build_prompt(doc_row, evidence_units: List[Dict[str, Any]]) -> str:
    (
        doc_id,
        raw_file_id,
        file_name,
        storage_url,
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
        "evidence_units": [
            {
                "evidence_unit_index": i + 1,
                "evidence_type": e["evidence_type"],
                "evidence_title": e["evidence_title"],
                "direct_quote": e["direct_quote"],
                "evidence_summary": e["evidence_summary"],
                "supports_claims": e["supports_claims"] or [],
                "does_not_support_claims": e["does_not_support_claims"] or [],
                "role_families": e["role_families"] or [],
                "competency_tags": e["competency_tags"] or [],
                "tool_tags": e["tool_tags"] or [],
                "project_tags": e["project_tags"] or [],
            }
            for i, e in enumerate(evidence_units)
        ],
    }

    return (
        "Synthesize draft profile asset(s) from these evidence units. "
        "Use only the evidence provided. Preserve limits. JSON only.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def delete_existing_draft_assets(cur, raw_file_id):
    cur.execute(
        """
        SELECT id
        FROM profile_assets
        WHERE created_from_raw_file_id = %s
          AND compiler_version = %s
          AND status = 'draft'
        """,
        (raw_file_id, VERSION),
    )
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return

    cur.execute(
        "DELETE FROM profile_asset_evidence_items WHERE profile_asset_id = ANY(%s)",
        (ids,),
    )
    cur.execute(
        "DELETE FROM profile_assets WHERE id = ANY(%s)",
        (ids,),
    )


def insert_asset(cur, doc_row, asset: Dict[str, Any], evidence_units: List[Dict[str, Any]]):
    (
        doc_id,
        raw_file_id,
        file_name,
        storage_url,
        document_title,
        document_type,
        source_role,
        document_summary,
        risk_notes,
    ) = doc_row

    cur.execute(
        """
        INSERT INTO profile_assets (
          asset_title,
          asset_type,
          abstraction_level,
          status,
          canonical_narrative,
          job_oriented_summary,
          resume_bullet_bank,
          interview_story,
          cover_letter_positioning,
          role_families,
          competency_tags,
          tool_tags,
          project_tags,
          do_not_overclaim_rules,
          created_from_raw_file_id,
          compiler_version,
          source_strategy,
          confidence,
          review_note
        )
        VALUES (
          %s, %s,
          'source_preserving_asset',
          'draft',
          %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s,
          %s,
          %s,
          'evidence_unit_asset_synthesis',
          %s,
          %s
        )
        RETURNING id
        """,
        (
            asset["asset_title"],
            asset["asset_type"],
            asset["canonical_narrative"],
            asset["job_oriented_summary"],
            asset["resume_bullet_bank"],
            asset["interview_story"],
            asset["cover_letter_positioning"],
            asset["role_families"],
            asset["competency_tags"],
            asset["tool_tags"],
            asset["project_tags"],
            asset["do_not_overclaim_rules"],
            raw_file_id,
            VERSION,
            asset["confidence"],
            "Draft asset generated from profile_evidence_units; requires grounding/overclaim audit before approval.",
        ),
    )
    asset_id = cur.fetchone()[0]

    links = asset.get("evidence_links") or []
    chosen = []

    for link in links:
        try:
            idx = int(link.get("evidence_unit_index")) - 1
            if 0 <= idx < len(evidence_units):
                chosen.append((int(link.get("evidence_rank") or len(chosen) + 1), evidence_units[idx]))
        except Exception:
            continue

    if not chosen:
        chosen = [(i + 1, e) for i, e in enumerate(evidence_units[:8])]

    seen = set()
    for rank, e in sorted(chosen, key=lambda x: x[0])[:10]:
        key = e["evidence_unit_id"]
        if key in seen:
            continue
        seen.add(key)

        evidence_text = e["evidence_summary"] or e["direct_quote"] or e["evidence_title"]

        cur.execute(
            """
            INSERT INTO profile_asset_evidence_items (
              profile_asset_id,
              raw_file_id,
              chunk_id,
              evidence_rank,
              evidence_type,
              section_title,
              evidence_text,
              source_file_name,
              source_path,
              page_hint
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                e["raw_file_id"],
                e["chunk_id"],
                rank,
                e["evidence_type"],
                e["section_title"],
                evidence_text,
                e["file_name"],
                e["storage_url"],
                f"section_index={e['section_index']}" if e["section_index"] is not None else None,
            ),
        )

    return asset_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--document-type", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-evidence-units", type=int, default=12)
    parser.add_argument("--max-assets-per-doc", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("===== PROFILE ASSET SYNTHESIZER QWEN V1 =====")
    print(f"Version:       {VERSION}")
    print(f"Mode:          {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Model:         {args.model}")
    print(f"Limit:         {args.limit}")
    print(f"Document type: {args.document_type}")
    print(f"Max assets/doc:{args.max_assets_per_doc}")
    print(f"Force:         {args.force}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            docs = fetch_docs(cur, args.limit, args.document_type, args.force)

            print(f"Documents selected: {len(docs)}")

            inserted_assets = 0
            failed_docs = 0

            for i, doc in enumerate(docs, start=1):
                doc_id, raw_file_id, file_name, storage_url, title, doc_type, source_role, *_ = doc
                evidence_units = fetch_evidence_units(cur, doc_id, args.max_evidence_units)
                fallback_type = fallback_asset_type(doc_type)

                print("")
                print(f"--- Document {i}/{len(docs)} ---")
                print(f"File:      {file_name}")
                print(f"Title:     {title}")
                print(f"Type:      {doc_type}")
                print(f"Role:      {source_role}")
                print(f"Evidence:  {len(evidence_units)}")

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT asset_synth_sp")
                try:
                    if args.force:
                        delete_existing_draft_assets(cur, raw_file_id)

                    prompt = build_prompt(doc, evidence_units)
                    result = call_ollama_json(prompt, args.model)

                    assets_raw = result.get("assets", [])
                    if not isinstance(assets_raw, list):
                        raise RuntimeError("Model output does not contain assets list.")

                    doc_assets = 0
                    for raw_asset in assets_raw[:max(1, min(args.max_assets_per_doc, 2))]:
                        asset = normalize_asset(raw_asset, fallback_type)
                        if not asset["canonical_narrative"]:
                            continue
                        ok, reason = validate_asset_basic(asset, doc, evidence_units)
                        if not ok:
                            print(f"Skipped asset: {reason} | {asset['asset_title']}")
                            continue

                        insert_asset(cur, doc, asset, evidence_units)
                        doc_assets += 1

                    cur.execute("RELEASE SAVEPOINT asset_synth_sp")
                    inserted_assets += doc_assets

                    print(f"Assets inserted: {doc_assets}")
                    conn.commit()

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT asset_synth_sp")
                    cur.execute("RELEASE SAVEPOINT asset_synth_sp")
                    failed_docs += 1
                    print(f"FAILED: {e}")

            if args.apply:
                conn.commit()

    print("")
    print("===== SUMMARY =====")
    print(f"Documents selected: {len(docs)}")
    print(f"Assets inserted:    {inserted_assets}")
    print(f"Failed documents:   {failed_docs}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
