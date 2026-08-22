import argparse
import json
import os
import re
import time
import urllib.request
from collections import defaultdict
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
DEFAULT_MODEL = _model_config.get_model("structured_asset_synth")

EUB_VERSION = "structured_evidence_unit_builder_qwen_v2_2026_04_27"
VERSION = "structured_tool_workflow_asset_synthesizer_qwen_v1_2026_04_27"


SYSTEM_PROMPT = """You are the Structured Tool Workflow Asset Synthesizer inside a job-application operating system.

Your job is to synthesize one rich profile asset from structured evidence units in the same workflow group.

Do NOT approve the asset.
Do NOT write a final resume.
Do NOT invent professional experience, production deployment, certifications, expert mastery, employment, awards, or publications.
Preserve evidence boundaries: academic lab, coursework, project use, material exposure, or job-market target.

Return JSON only:
{
  "asset": {
    "asset_title": "specific workflow asset title",
    "canonical_narrative": "rich source-preserving narrative",
    "job_oriented_summary": "how this asset can be used in job applications",
    "resume_bullet_bank": ["safe draft bullet", "safe phrase"],
    "interview_story": "interview explanation, bounded by evidence",
    "cover_letter_positioning": "safe cover letter positioning",
    "role_families": ["DFIR", "SOC_analyst", "GRC", "AppSec", "NetworkSecurity", "SecurityAnalytics"],
    "competency_tags": ["specific competency"],
    "tool_tags": ["tools actually supported"],
    "project_tags": ["workflow_group", "source tags"],
    "do_not_overclaim_rules": ["specific boundary"],
    "confidence": 0.80,
    "evidence_links": [
      {"evidence_unit_index": 1, "evidence_rank": 1, "evidence_role": "primary|supporting|boundary"}
    ]
  }
}
"""


FORBIDDEN_POSITIVE_TERMS = [
    "professional experience",
    "production experience",
    "production deployment",
    "enterprise deployment",
    "expert",
    "mastery",
    "certified",
    "certification earned",
    "employed as",
    "worked professionally",
    "award-winning",
    "published",
]


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def lower_norm(value: Any) -> str:
    return norm(value).lower()


def clean_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [norm(x) for x in value if norm(x)]
    if isinstance(value, str) and norm(value):
        return [norm(value)]
    return []


def text_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(f"- {norm(x)}" for x in value if norm(x))
    return norm(value)


def clamp_float(value: Any, default: float = 0.80) -> float:
    try:
        x = float(value)
        return max(0.0, min(1.0, x))
    except Exception:
        return default


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
                role="structured_asset_synth",
                model=model,
                local_url=OLLAMA_URL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                timeout=900,
                temperature=0.05,
                num_ctx=12000,
                json_mode=True,
            )
            return parse_json_content(content)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"LLM JSON call failed after {retries} retries: {last_error}")


def fetch_units(cur, file_like: str, workflow_group: Optional[str], limit_units: int) -> List[Dict[str, Any]]:
    params: List[Any] = [EUB_VERSION, f"%{file_like}%"]

    workflow_sql = ""
    if workflow_group:
        workflow_sql = "AND peu.workflow_group = %s"
        params.append(workflow_group)

    params.append(limit_units)

    cur.execute(
        f"""
        SELECT
          peu.id,
          peu.profile_document_section_id,
          peu.raw_file_id,
          peu.chunk_id,
          rf.file_name,
          rf.storage_url,
          pds.section_index,
          pds.section_title,

          peu.claim,
          peu.claim_type,
          peu.tool_name,
          peu.tool_category,
          peu.workflow_group,
          peu.evidence_strength,
          peu.evidence_summary,
          peu.resume_safe_phrase,
          peu.role_relevance,
          peu.must_not_claim,
          peu.supports_claims,
          peu.does_not_support_claims,
          peu.competency_tags,
          peu.tool_tags,
          peu.project_tags,
          peu.source_boundaries,
          peu.source_confidence,
          peu.grounding_confidence,
          peu.created_at
        FROM profile_evidence_units peu
        JOIN raw_files rf
          ON rf.id = peu.raw_file_id
        LEFT JOIN profile_document_sections pds
          ON pds.id = peu.profile_document_section_id
        WHERE peu.builder_version = %s
          AND rf.file_name ILIKE %s
          AND peu.status = 'draft'
          AND peu.workflow_group IS NOT NULL
          AND peu.workflow_group <> ''
          AND peu.workflow_group <> 'unknown'

          -- ASYN should synthesize workflow assets from operational evidence,
          -- not from job-market role clusters or broad positioning notes.
          AND peu.evidence_strength IN ('direct_lab_use', 'project_use')
          AND peu.claim_type NOT IN (
            'job_market_target',
            'role_positioning',
            'resume_safe_phrase',
            'must_not_claim'
          )

          -- Exclude role-market sections like 16.1 SOC Analyst, 16.4 DFIR Junior Role.
          AND COALESCE(pds.section_title, '') !~ '^16[.]'

          {workflow_sql}
        ORDER BY
          peu.workflow_group,
          pds.section_index,
          CASE WHEN peu.resume_safe_phrase IS NOT NULL AND length(trim(peu.resume_safe_phrase)) > 0 THEN 0 ELSE 1 END,
          peu.created_at DESC
        LIMIT %s
        """,
        params,
    )

    keys = [
        "id",
        "profile_document_section_id",
        "raw_file_id",
        "chunk_id",
        "file_name",
        "storage_url",
        "section_index",
        "section_title",
        "claim",
        "claim_type",
        "tool_name",
        "tool_category",
        "workflow_group",
        "evidence_strength",
        "evidence_summary",
        "resume_safe_phrase",
        "role_relevance",
        "must_not_claim",
        "supports_claims",
        "does_not_support_claims",
        "competency_tags",
        "tool_tags",
        "project_tags",
        "source_boundaries",
        "source_confidence",
        "grounding_confidence",
        "created_at",
    ]

    rows = [dict(zip(keys, row)) for row in cur.fetchall()]

    # Dedup on read: keep one best evidence unit per structured section.
    seen_sections = set()
    deduped: List[Dict[str, Any]] = []

    for row in rows:
        sid = row["profile_document_section_id"]
        if sid in seen_sections:
            continue
        seen_sections.add(sid)
        deduped.append(row)

    return deduped


def group_units(units: List[Dict[str, Any]], min_units: int) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for unit in units:
        grouped[unit["workflow_group"]].append(unit)

    return {
        k: v
        for k, v in grouped.items()
        if len(v) >= min_units
    }


def existing_asset_for_workflow(cur, workflow_group: str) -> Optional[str]:
    cur.execute(
        """
        SELECT id::text
        FROM profile_assets
        WHERE compiler_version = %s
          AND status = 'draft'
          AND project_tags @> ARRAY[%s]::text[]
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (VERSION, workflow_group),
    )
    row = cur.fetchone()
    return row[0] if row else None


def delete_existing_workflow_asset(cur, workflow_group: str):
    cur.execute(
        """
        SELECT id
        FROM profile_assets
        WHERE compiler_version = %s
          AND status = 'draft'
          AND project_tags @> ARRAY[%s]::text[]
        """,
        (VERSION, workflow_group),
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


def build_prompt(workflow_group: str, units: List[Dict[str, Any]]) -> str:
    payload = {
        "workflow_group": workflow_group,
        "evidence_units": [
            {
                "evidence_unit_index": i + 1,
                "section": u["section_title"],
                "tool_name": u["tool_name"],
                "claim_type": u["claim_type"],
                "tool_category": u["tool_category"],
                "workflow_group": u["workflow_group"],
                "evidence_strength": u["evidence_strength"],
                "claim": u["claim"],
                "evidence_summary": u["evidence_summary"],
                "resume_safe_phrase": u["resume_safe_phrase"],
                "role_relevance": u["role_relevance"] or [],
                "must_not_claim": u["must_not_claim"] or [],
                "supports_claims": u["supports_claims"] or [],
                "does_not_support_claims": u["does_not_support_claims"] or [],
                "source_boundaries": u["source_boundaries"] or {},
            }
            for i, u in enumerate(units)
        ],
    }

    return (
        "Synthesize one draft tool-workflow profile asset from these evidence units. "
        "Use only this evidence. Preserve lab/course/project boundaries and do-not-overclaim rules. "
        "Return JSON only.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    )


def normalize_asset(raw: Dict[str, Any], workflow_group: str, units: List[Dict[str, Any]]) -> Dict[str, Any]:
    title = norm(raw.get("asset_title")) or f"{workflow_group.replace('_', ' ').title()} Tool Workflow Asset"

    role_families = clean_list(raw.get("role_families"))
    if not role_families:
        for u in units:
            role_families.extend(clean_list(u.get("role_relevance")))
    role_families = list(dict.fromkeys(role_families))

    competency_tags = clean_list(raw.get("competency_tags"))
    for u in units:
        competency_tags.extend(clean_list(u.get("competency_tags")))
    competency_tags = list(dict.fromkeys(competency_tags))

    tool_tags = clean_list(raw.get("tool_tags"))
    for u in units:
        tool_tags.extend(clean_list(u.get("tool_tags")) or [u.get("tool_name")])
    tool_tags = [x for x in list(dict.fromkeys(tool_tags)) if x]

    project_tags = clean_list(raw.get("project_tags"))
    project_tags.extend([workflow_group, "structured_tool_inventory", EUB_VERSION])
    for u in units:
        project_tags.extend(clean_list(u.get("project_tags")))
    project_tags = list(dict.fromkeys(project_tags))

    # PATCH H3: moi phan tu phai la CAU CAM. Danh tu tran duoc nang thanh
    # "Do not claim X."; enum/tag ky thuat (direct_lab_use, pki_tls) bi loai.
    do_not = _safety.build_overclaim_rules(
        raw.get("do_not_overclaim_rules"),
        *[u.get("must_not_claim") for u in units],
        *[u.get("does_not_support_claims") for u in units],
    )

    return {
        "asset_title": title[:300],
        "asset_type": "tool_workflow_asset",
        "canonical_narrative": text_field(raw.get("canonical_narrative"))[:9000],
        "job_oriented_summary": text_field(raw.get("job_oriented_summary"))[:5000],
        "resume_bullet_bank": text_field(raw.get("resume_bullet_bank"))[:6000],
        "interview_story": text_field(raw.get("interview_story"))[:5000],
        "cover_letter_positioning": text_field(raw.get("cover_letter_positioning"))[:4000],
        "role_families": role_families,
        "competency_tags": competency_tags,
        "tool_tags": tool_tags,
        "project_tags": project_tags,
        "do_not_overclaim_rules": do_not,
        "confidence": clamp_float(raw.get("confidence"), 0.80),
        "evidence_links": raw.get("evidence_links") if isinstance(raw.get("evidence_links"), list) else [],
    }


def validate_asset(asset: Dict[str, Any], workflow_group: str, units: List[Dict[str, Any]]) -> tuple[bool, str]:
    if len(units) < 1:
        return False, "no_evidence_units"

    if len(asset["canonical_narrative"].strip()) < 180:
        return False, "canonical_narrative_too_short"

    positive_text = " ".join([
        asset["canonical_narrative"],
        asset["job_oriented_summary"],
        asset["resume_bullet_bank"],
        asset["interview_story"],
        asset["cover_letter_positioning"],
    ]).lower()

    for term in FORBIDDEN_POSITIVE_TERMS:
        if term in positive_text:
            if f"do not claim {term}" not in positive_text and f"without claiming {term}" not in positive_text:
                return False, f"forbidden_positive_term:{term}"

    if workflow_group not in asset["project_tags"]:
        return False, "workflow_group_missing_from_project_tags"

    return True, "ok"


def insert_asset(cur, workflow_group: str, asset: Dict[str, Any], units: List[Dict[str, Any]], model: str) -> str:
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
          NULL,
          %s,
          'structured_workflow_synthesis_from_eub_v2',
          %s,
          %s
        )
        RETURNING id::text
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
            VERSION,
            asset["confidence"],
            f"Draft structured workflow asset for {workflow_group}; requires AUD before approval. Model={model}.",
        ),
    )
    asset_id = cur.fetchone()[0]

    links = asset.get("evidence_links") or []
    chosen = []

    for link in links:
        try:
            idx = int(link.get("evidence_unit_index")) - 1
            if 0 <= idx < len(units):
                rank = int(link.get("evidence_rank") or len(chosen) + 1)
                chosen.append((rank, units[idx]))
        except Exception:
            continue

    if not chosen:
        chosen = [(i + 1, u) for i, u in enumerate(units[:12])]

    seen_sections = set()
    for rank, u in sorted(chosen, key=lambda x: x[0])[:12]:
        sid = u["profile_document_section_id"]
        if sid in seen_sections:
            continue
        seen_sections.add(sid)

        evidence_text = u["evidence_summary"] or u["claim"] or u["tool_name"]

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
                u["raw_file_id"],
                u["chunk_id"],
                rank,
                "tool_workflow",
                u["section_title"],
                evidence_text,
                u["file_name"],
                u["storage_url"],
                f"section_index={u['section_index']}",
            ),
        )

    return asset_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--file-like", default="tool_inventory")
    parser.add_argument("--workflow-group", default=None)
    parser.add_argument("--limit-workflows", type=int, default=2)
    parser.add_argument("--limit-units", type=int, default=500)
    parser.add_argument("--min-units", type=int, default=2)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    print("===== STRUCTURED TOOL WORKFLOW ASSET SYNTHESIZER QWEN V1 =====")
    print(f"Version:         {VERSION}")
    print(f"Mode:            {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"File like:       {args.file_like}")
    print(f"Workflow group:  {args.workflow_group}")
    print(f"Limit workflows: {args.limit_workflows}")
    print(f"Min units:       {args.min_units}")
    print(f"Model:           {args.model}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            units = fetch_units(cur, args.file_like, args.workflow_group, args.limit_units)
            grouped = group_units(units, args.min_units)

            workflow_items = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:args.limit_workflows]

            print(f"Evidence units read after dedup: {len(units)}")
            print(f"Workflow groups eligible:        {len(grouped)}")
            print(f"Workflow groups selected:        {len(workflow_items)}")

            inserted = 0
            skipped = 0
            failed = 0

            for workflow_group, group_units_list in workflow_items:
                print("")
                print(f"--- Workflow: {workflow_group} ---")
                print(f"Evidence units: {len(group_units_list)}")
                print("Tools:", ", ".join([norm(u.get("tool_name")) for u in group_units_list[:12] if norm(u.get("tool_name"))]))

                existing = existing_asset_for_workflow(cur, workflow_group)
                if existing and not args.force:
                    print(f"SKIP: draft asset already exists: {existing[:8]}")
                    skipped += 1
                    continue

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT structured_asset_sp")
                try:
                    if args.force:
                        delete_existing_workflow_asset(cur, workflow_group)

                    prompt = build_prompt(workflow_group, group_units_list[:16])
                    result = call_ollama_json(prompt, args.model)

                    raw_asset = result.get("asset")
                    if not isinstance(raw_asset, dict):
                        raise RuntimeError("Model output missing asset object.")

                    asset = normalize_asset(raw_asset, workflow_group, group_units_list)
                    ok, reason = validate_asset(asset, workflow_group, group_units_list)

                    if not ok:
                        print(f"SKIP asset validation: {reason}")
                        cur.execute("ROLLBACK TO SAVEPOINT structured_asset_sp")
                        cur.execute("RELEASE SAVEPOINT structured_asset_sp")
                        skipped += 1
                        continue

                    asset_id = insert_asset(cur, workflow_group, asset, group_units_list, args.model)
                    cur.execute("RELEASE SAVEPOINT structured_asset_sp")
                    conn.commit()

                    inserted += 1
                    print(f"Inserted asset: {asset_id[:8]} | {asset['asset_title']}")

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT structured_asset_sp")
                    cur.execute("RELEASE SAVEPOINT structured_asset_sp")
                    failed += 1
                    print(f"FAILED: {e}")

    print("")
    print("===== SUMMARY =====")
    print(f"Inserted: {inserted}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
