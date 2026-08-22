import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/profile-ingestion/<this file>.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.model_config import get_model  # noqa: E402
from services.common.llm_gateway import chat_text  # noqa: E402


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
DEFAULT_MODEL = get_model("profile_asset_auditor")

ASSET_COMPILER_VERSION = "structured_tool_workflow_asset_synthesizer_qwen_v1_2026_04_27"
REQUIRED_DETERMINISTIC_AUDIT_TYPE = "deterministic_structured_asset_audit"
REQUIRED_DETERMINISTIC_AUDIT_VERSION = "structured_asset_deterministic_audit_v1_2026_04_27"

AUDIT_TYPE = "deepseek_structured_asset_grounding_overclaim_audit"
AUDIT_VERSION = "deepseek_structured_asset_audit_v1_2026_04_27"


SYSTEM_PROMPT = """You are the grounding and overclaim auditor for a job-application operating system.

Audit one draft profile asset against its linked evidence items.

Return JSON only. Do not rewrite the asset.

Required JSON:
{
  "grounding_status": "grounded|needs_review|blocked",
  "overclaim_risk": "low|medium|high",
  "information_loss_risk": "low|medium|high",
  "evidence_coverage_score": 0.0,
  "specificity_score": 0.0,
  "job_relevance_score": 0.0,
  "supported_claims": ["claim supported by evidence"],
  "unsupported_claims": ["claim not supported or overclaimed"],
  "required_edits": ["required edit before approval"],
  "audit_notes": "short audit explanation"
}

Audit rules:
- Academic labs, coursework, and projects must not be represented as professional employment.
- Do not allow claims of production deployment, certification, expert mastery, or enterprise ownership unless explicitly evidenced.
- Resume bullets may be draft-safe if bounded to labs/projects/coursework.
- If evidence items support the asset and only boundary wording is conservative, mark grounded/low.
- If a claim is plausible but too broad, mark needs_review/medium.
- If positive-facing text claims professional/production/certified/expert without evidence, mark blocked/high.
"""


FORBIDDEN_POSITIVE_TERMS = [
    "professional experience",
    "production experience",
    "production deployment",
    "enterprise deployment",
    "certified",
    "certification earned",
    "expert",
    "mastery",
    "employed as",
    "worked professionally",
]


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [norm(x) for x in value if norm(x)]
    if isinstance(value, str) and norm(value):
        return [norm(value)]
    return []


def clamp_score(value: Any, default: float) -> float:
    try:
        x = float(value)
        return max(0.0, min(1.0, x))
    except Exception:
        return default


def strip_thinking(text: str) -> str:
    # Handles normal <think>...</think> and incomplete thinking blocks.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S | re.I)
    return text.strip()


def extract_json_object(text: str) -> Dict[str, Any]:
    text = strip_thinking(text)
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    # Robust balanced-brace extraction: try every JSON-looking object.
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in reversed(starts):
        depth = 0
        in_str = False
        esc = False

        for end in range(start, len(text)):
            ch = text[end]

            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:end + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break

    raise ValueError("Could not extract JSON object from model output.")


def call_ollama_json(prompt: str, model: str, retries: int = 2) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            content = chat_text(
                role="profile_asset_auditor",
                model=model,
                local_url=OLLAMA_URL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                timeout=1500,
                temperature=0.02,
                num_ctx=12000,
                json_mode=True,
            )
            return extract_json_object(content)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(5 * attempt)

    raise RuntimeError(f"LLM JSON call failed after {retries} retries: {last_error}")


def fetch_assets(cur, limit: int, force: bool) -> List[Dict[str, Any]]:
    existing_filter = ""
    if not force:
        existing_filter = """
          AND NOT EXISTS (
            SELECT 1
            FROM profile_asset_audits daa
            WHERE daa.profile_asset_id = pa.id
              AND daa.audit_type = %s
              AND daa.audit_version = %s
          )
        """
        # Placeholder order in SQL:
        # 1 det.audit_type
        # 2 det.audit_version
        # 3 pa.compiler_version
        # 4 existing deep audit_type
        # 5 existing deep audit_version
        # 6 limit
        params: Tuple[Any, ...] = (
            REQUIRED_DETERMINISTIC_AUDIT_TYPE,
            REQUIRED_DETERMINISTIC_AUDIT_VERSION,
            ASSET_COMPILER_VERSION,
            AUDIT_TYPE,
            AUDIT_VERSION,
            limit,
        )
    else:
        # Placeholder order in SQL:
        # 1 det.audit_type
        # 2 det.audit_version
        # 3 pa.compiler_version
        # 4 limit
        params = (
            REQUIRED_DETERMINISTIC_AUDIT_TYPE,
            REQUIRED_DETERMINISTIC_AUDIT_VERSION,
            ASSET_COMPILER_VERSION,
            limit,
        )

    cur.execute(
        f"""
        SELECT
          pa.id::text,
          pa.asset_title,
          pa.asset_type,
          pa.canonical_narrative,
          pa.job_oriented_summary,
          pa.resume_bullet_bank,
          pa.interview_story,
          pa.cover_letter_positioning,
          pa.role_families,
          pa.competency_tags,
          pa.tool_tags,
          pa.project_tags,
          pa.do_not_overclaim_rules,
          pa.confidence
        FROM profile_assets pa
        JOIN profile_asset_audits det
          ON det.profile_asset_id = pa.id
         AND det.audit_type = %s
         AND det.audit_version = %s
         AND det.grounding_status = 'grounded'
         AND det.overclaim_risk = 'low'
        WHERE pa.compiler_version = %s
          AND pa.status = 'draft'
          {existing_filter}
        ORDER BY pa.created_at
        LIMIT %s
        """,
        params,
    )

    keys = [
        "id",
        "asset_title",
        "asset_type",
        "canonical_narrative",
        "job_oriented_summary",
        "resume_bullet_bank",
        "interview_story",
        "cover_letter_positioning",
        "role_families",
        "competency_tags",
        "tool_tags",
        "project_tags",
        "do_not_overclaim_rules",
        "confidence",
    ]

    return [dict(zip(keys, row)) for row in cur.fetchall()]


def fetch_evidence_items(cur, asset_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
          evidence_rank,
          evidence_type,
          section_title,
          evidence_text,
          source_file_name,
          source_path,
          page_hint
        FROM profile_asset_evidence_items
        WHERE profile_asset_id = %s
        ORDER BY evidence_rank
        """,
        (asset_id,),
    )

    keys = [
        "evidence_rank",
        "evidence_type",
        "section_title",
        "evidence_text",
        "source_file_name",
        "source_path",
        "page_hint",
    ]

    return [dict(zip(keys, row)) for row in cur.fetchall()]


def build_prompt(asset: Dict[str, Any], evidence_items: List[Dict[str, Any]]) -> str:
    payload = {
        "asset": asset,
        "evidence_items": evidence_items,
        "audit_focus": [
            "grounding",
            "overclaim risk",
            "information loss",
            "specificity",
            "job relevance",
            "approval gate readiness",
        ],
    }

    return (
        "Audit this draft profile asset against its linked evidence items. "
        "Return JSON only. Do not rewrite the asset.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    )


def fallback_audit(asset: Dict[str, Any], evidence_items: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    evidence_count = len(evidence_items)
    titles = [norm(x.get("section_title")) for x in evidence_items if norm(x.get("section_title"))]

    return {
        "grounding_status": "grounded" if evidence_count >= 2 else "needs_review",
        "overclaim_risk": "low",
        "information_loss_risk": "low" if evidence_count >= 2 else "medium",
        "evidence_coverage_score": min(1.0, evidence_count / 8.0) if evidence_count else 0.0,
        "specificity_score": 0.75,
        "job_relevance_score": 0.75,
        "supported_claims": titles[:12],
        "unsupported_claims": [],
        "required_edits": [],
        "audit_notes": f"Fallback deterministic deep audit used because model audit failed: {reason[:500]}",
    }


def deterministic_safety_adjustment(asset: Dict[str, Any], audit: Dict[str, Any], evidence_count: int) -> Dict[str, Any]:
    positive_text = " ".join([
        norm(asset.get("canonical_narrative")),
        norm(asset.get("job_oriented_summary")),
        norm(asset.get("resume_bullet_bank")),
        norm(asset.get("interview_story")),
        norm(asset.get("cover_letter_positioning")),
    ]).lower()

    has_forbidden = any(term in positive_text for term in FORBIDDEN_POSITIVE_TERMS)

    grounding_status = norm(audit.get("grounding_status")) or "needs_review"
    overclaim_risk = norm(audit.get("overclaim_risk")) or "medium"
    information_loss_risk = norm(audit.get("information_loss_risk")) or "low"

    if grounding_status not in {"grounded", "needs_review", "blocked"}:
        grounding_status = "needs_review"
    if overclaim_risk not in {"low", "medium", "high"}:
        overclaim_risk = "medium"
    if information_loss_risk not in {"low", "medium", "high"}:
        information_loss_risk = "low"

    required_edits = clean_list(audit.get("required_edits"))
    unsupported_claims = clean_list(audit.get("unsupported_claims"))

    if evidence_count == 0:
        grounding_status = "blocked"
        information_loss_risk = "high"
        required_edits.append("Attach evidence items before approval.")
        unsupported_claims.append("Asset has no linked evidence items.")

    if has_forbidden:
        grounding_status = "blocked"
        overclaim_risk = "high"
        required_edits.append("Remove or rewrite positive-facing professional/production/certification/expert language before approval.")

    audit["grounding_status"] = grounding_status
    audit["overclaim_risk"] = overclaim_risk
    audit["information_loss_risk"] = information_loss_risk
    audit["evidence_coverage_score"] = clamp_score(audit.get("evidence_coverage_score"), 0.75 if evidence_count else 0.0)
    audit["specificity_score"] = clamp_score(audit.get("specificity_score"), 0.75)
    audit["job_relevance_score"] = clamp_score(audit.get("job_relevance_score"), 0.75)
    audit["supported_claims"] = clean_list(audit.get("supported_claims"))
    audit["unsupported_claims"] = list(dict.fromkeys(unsupported_claims))
    audit["required_edits"] = list(dict.fromkeys(required_edits))
    audit["audit_notes"] = norm(audit.get("audit_notes"))[:4000] or "Deep audit completed."

    return audit


def insert_audit(cur, asset_id: str, audit: Dict[str, Any], model: str):
    cur.execute(
        """
        DELETE FROM profile_asset_audits
        WHERE profile_asset_id = %s
          AND audit_type = %s
          AND audit_version = %s
        """,
        (asset_id, AUDIT_TYPE, AUDIT_VERSION),
    )

    cur.execute(
        """
        INSERT INTO profile_asset_audits (
          profile_asset_id,
          audit_type,
          audit_model,
          audit_version,
          grounding_status,
          overclaim_risk,
          information_loss_risk,
          evidence_coverage_score,
          specificity_score,
          job_relevance_score,
          supported_claims,
          unsupported_claims,
          required_edits,
          audit_notes
        )
        VALUES (
          %s, %s, %s, %s,
          %s, %s, %s,
          %s, %s, %s,
          %s, %s, %s, %s
        )
        """,
        (
            asset_id,
            AUDIT_TYPE,
            model,
            AUDIT_VERSION,
            audit["grounding_status"],
            audit["overclaim_risk"],
            audit["information_loss_risk"],
            audit["evidence_coverage_score"],
            audit["specificity_score"],
            audit["job_relevance_score"],
            audit["supported_claims"],
            audit["unsupported_claims"],
            audit["required_edits"],
            audit["audit_notes"],
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--allow-fallback", action="store_true")
    args = parser.parse_args()

    print("===== DEEPSEEK STRUCTURED ASSET AUDITOR V1 =====")
    print(f"Audit type:    {AUDIT_TYPE}")
    print(f"Audit version: {AUDIT_VERSION}")
    print(f"Mode:          {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Force:         {args.force}")
    print(f"Model:         {args.model}")
    print(f"Limit:         {args.limit}")
    print(f"Fallback:      {args.allow_fallback}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            assets = fetch_assets(cur, args.limit, args.force)
            print(f"Assets selected: {len(assets)}")

            inserted = 0
            skipped = 0
            failed = 0

            for i, asset in enumerate(assets, start=1):
                asset_id = asset["id"]
                evidence_items = fetch_evidence_items(cur, asset_id)

                print("")
                print(f"--- Asset {i}/{len(assets)} ---")
                print(f"Asset:    {asset['asset_title']}")
                print(f"Evidence: {len(evidence_items)}")

                if not args.apply:
                    skipped += 1
                    continue

                cur.execute("SAVEPOINT deep_audit_sp")
                try:
                    prompt = build_prompt(asset, evidence_items)
                    try:
                        audit = call_ollama_json(prompt, args.model)
                        model_used = args.model
                    except Exception as model_error:
                        if not args.allow_fallback:
                            raise
                        audit = fallback_audit(asset, evidence_items, str(model_error))
                        model_used = f"{args.model}+deterministic_fallback"

                    audit = deterministic_safety_adjustment(asset, audit, len(evidence_items))
                    insert_audit(cur, asset_id, audit, model_used)

                    cur.execute("RELEASE SAVEPOINT deep_audit_sp")
                    conn.commit()

                    inserted += 1
                    print(
                        "Inserted audit:",
                        audit["grounding_status"],
                        audit["overclaim_risk"],
                        audit["information_loss_risk"],
                    )

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT deep_audit_sp")
                    cur.execute("RELEASE SAVEPOINT deep_audit_sp")
                    failed += 1
                    print(f"FAILED: {e}")

    print("")
    print("===== SUMMARY =====")
    print(f"Inserted: {inserted}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
