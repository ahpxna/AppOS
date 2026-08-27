"""
L6 -- DOCUMENT GENERATION
Resume Agent / Cover Letter Agent / Short Answer Agent.

Grounding contract:
  - Reads ONLY from v_document_generation_source_assets (approved assets).
  - Every generated claim must name the profile_asset_id it came from.
  - Claims citing an unknown asset id are dropped before persistence.
  - Output is written to generated_documents with qa_status = NULL,
    which puts it on the truth checker's queue. Nothing is approved here.

Usage:
  python services/document-generation/generate_documents_v1.py \
      --application-id <uuid> --doc-type resume
  python services/document-generation/generate_documents_v1.py \
      --application-id <uuid> --doc-type short_answers \
      --question "Why do you want to work here?"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly. Without this, the import below raises
# ModuleNotFoundError unless the caller happens to have the repo root on
# PYTHONPATH already. Confirmed live 2026-08-01 (this file was already
# broken this way before today's fix).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.observability import emit_trace, make_trace_id
from services.common.company_research_sources import (
    company_research_field_evidence, company_research_source_urls,
)
from services.common.llm_gateway import generate_text, resolve_config
from services.common.model_config import get_model
from services.common.project_registry import ProjectRegistryError, project_asset_terms_by_slot
from services.common.resume_project_bullet_audit import (
    ResumeBulletAuditError, load_template_bullet_baselines, normalize as normalize_bullet,
    validate_bullet_change,
)
from services.common.resume_project_header_audit import (
    ResumeHeaderAuditError, load_template_subtitle_baselines, normalize as normalize_subtitle,
    validate_subtitle_change,
)
from services.common.resume_experience_bullet_audit import (
    ResumeExperienceAuditError, load_template_experience_baselines,
    normalize as normalize_experience, validate_experience_bullet_change,
)
from services.common.document_prompt_templates_v1 import (
    RESUME_TARGET_COVERAGE_PERCENT, COVER_POSITIONING_TARGET_PERCENT,
    build_resume_tailoring_prompt, build_cover_alignment_blueprint_prompt,
    build_cover_alignment_audit_prompt, build_cover_letter_tailoring_prompt,
    material_requirement_summary, requirement_catalog,
)
from services.common.config import database_dsn
from services.control_plane.document_attempts import (
    DocumentAttemptError, claim as claim_document_attempt,
    complete as complete_document_attempt, fail as fail_document_attempt,
)
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER

GENERATOR_VERSION = "document_generator_v5_recoverable_attempts_2026_08_26"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = get_model("docgen")

DOC_TYPES = ("resume", "cover_letter", "short_answers")
FIXED_RESUME_PROJECTS = (
    "1-2 CAROECT-D", "3-4 CIG-AMF", "5-6 PKI Sentinel", "7-8 ApplyOps",
    "9-10 Enterprise NetSec IaC", "11-12 Optimixer",
)
# Fallback only. The user-owned project registry is the normal source for these
# aliases; this map keeps historical installs safe before the first form save.
FIXED_RESUME_PROJECT_ASSET_TERMS = {
    1: ("caroect",),
    3: ("cig-amf", "cig amf"),
    5: ("pki sentinel", "pki-sentinel"),
    7: ("applyops", "apply ops"),
    9: ("enterprise netsec", "netsec iac", "network security iac"),
    11: ("optimixer",),
}

COMPONENT_BY_DOC_TYPE = {
    "resume": "resume_agent",
    "cover_letter": "cover_letter_agent",
    "short_answers": "short_answer_agent",
}


# ---------------------------------------------------------------- utilities

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def extract_json_object(raw: str) -> Dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    cleaned = cleaned.replace("```json", "```").replace("```JSON", "```").strip()

    fence = re.search(r"```(.*?)```", cleaned, flags=re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in model output.")
    parsed = json.loads(cleaned[first:last + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Model output JSON must be an object.")
    return parsed


def ollama_generate(
    *, model: str, prompt: str, ollama_url: str,
    timeout: int, temperature: float, num_ctx: int,
) -> str:
    return generate_text(role="docgen", model=model, prompt=prompt,
                         local_url=ollama_url, timeout=timeout,
                         temperature=temperature, num_ctx=num_ctx)


# ---------------------------------------------------------------- data access

def fetch_application_context(cur, application_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT
          a.id::text, a.company, a.job_title, a.jd_text,
          jfa.fit_score, jfa.fit_decision, jfa.role_family, jfa.seniority_level,
          jfa.matched_requirements, jfa.missing_or_weak_requirements,
          jfa.hard_blockers, jfa.risk_flags
        FROM applications a
        LEFT JOIN job_fit_analyses jfa ON jfa.application_id = a.id
        WHERE a.id = %s
        ORDER BY jfa.created_at DESC
        LIMIT 1;
        """,
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Application not found: {application_id}")

    if row[5] is None:
        raise RuntimeError(
            "This application has no fit analysis yet. "
            "Run analyze_job_fit_v1.py --apply first."
        )

    if row[5] == "reject":
        raise RuntimeError(
            f"Fit decision is 'reject' (score {row[4]}). "
            "Document generation is blocked for rejected applications. "
            "Override intentionally with --force if you disagree with the verdict."
        )

    app = {
        "id": row[0], "company": row[1], "job_title": row[2], "jd_text": row[3] or "",
        "fit_score": row[4], "fit_decision": row[5],
        "role_family": row[6], "seniority_level": row[7],
        "matched_requirements": row[8] or [],
        "missing_or_weak_requirements": row[9] or [],
        "hard_blockers": row[10] or [],
        "risk_flags": row[11] or [],
    }
    app["company_context"] = fetch_company_context(cur, app["company"])
    return app


def fetch_company_context(cur, company: Optional[str]) -> Dict[str, Any]:
    """Return fresh, source-bearing company facts for cover-letter motivation.

    This is intentionally separate from profile assets: company facts never
    become candidate evidence and an unavailable/stale cache simply yields an
    empty context rather than blocking document generation.
    """
    if not company:
        return {}
    cur.execute(
        """
        SELECT company_domain, summary, mission, products, recent_news, sources
        FROM company_research_cache
        WHERE lower(company_name) = lower(%s)
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY last_refreshed_at DESC NULLS LAST, created_at DESC
        LIMIT 1;
        """,
        (company,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    sources = sorted(company_research_source_urls(row[5]))
    if not sources:
        return {}
    field_evidence = company_research_field_evidence(row[5])
    # New research rows bind generated company facts to source excerpts. Legacy
    # rows remain readable for URL compatibility, but their free-text fields are
    # not promoted into a new employer-facing claim until refreshed under the
    # stronger evidence contract.
    evidence_aware = isinstance(row[5], dict) and "field_evidence" in row[5]
    return {
        "company_domain": row[0] or "",
        "summary": (row[1] or "") if evidence_aware and field_evidence.get("summary") else "",
        "mission": (row[2] or "") if evidence_aware and field_evidence.get("mission") else "",
        "products": (row[3] or "") if evidence_aware and field_evidence.get("products") else "",
        "recent_news": [
            item for item in (row[4] or [])
            if isinstance(item, dict)
            and item.get("source_url") in sources
            and str(item.get("supporting_quote") or "").strip()
        ],
        "sources": sources,
        "field_evidence": field_evidence,
    }


def fetch_source_assets(cur, role_family: Optional[str]) -> List[Dict[str, Any]]:
    """Approved assets only. Role-matched first, then everything else."""
    cur.execute(
        """
        SELECT
          profile_asset_id::text, asset_title, asset_type,
          role_families, competency_tags, tool_tags,
          job_oriented_summary, resume_bullet_bank,
          cover_letter_positioning, do_not_overclaim_rules
        FROM v_document_generation_source_assets
        ORDER BY
          CASE WHEN %s = ANY(role_families) THEN 0 ELSE 1 END,
          confidence DESC NULLS LAST,
          asset_title;
        """,
        (role_family or "",),
    )
    return [
        {
            "profile_asset_id": r[0], "asset_title": r[1], "asset_type": r[2],
            "role_families": r[3] or [], "competency_tags": r[4] or [],
            "tool_tags": r[5] or [], "job_oriented_summary": r[6] or "",
            "resume_bullet_bank": r[7] or "",
            "cover_letter_positioning": r[8] or "",
            "do_not_overclaim_rules": r[9] or [],
        }
        for r in cur.fetchall()
    ]


def render_asset_catalog(assets: List[Dict[str, Any]], *, field: str) -> str:
    blocks = []
    for a in assets:
        body = a.get(field) or a.get("job_oriented_summary") or ""
        if not body.strip():
            continue
        rules = "; ".join(a["do_not_overclaim_rules"]) or "none recorded"
        blocks.append(
            f"[ASSET {a['profile_asset_id']}] {a['asset_title']}\n"
            f"  type: {a['asset_type']}\n"
            f"  tools: {', '.join(a['tool_tags']) or 'n/a'}\n"
            f"  competencies: {', '.join(a['competency_tags']) or 'n/a'}\n"
            f"  MUST NOT CLAIM: {rules}\n"
            f"  source material:\n{body.strip()}\n"
        )
    return "\n".join(blocks)


def fixed_project_asset_ids(assets: List[Dict[str, Any]]) -> Dict[int, set[str]]:
    """Map each resume block's primary slot to its own approved profile assets.

    This prevents an otherwise approved asset for one project being written
    under another project's immutable title.  Asset titles intentionally act as
    the approval binding so unknown naming fails closed rather than guessing.
    """
    try:
        terms_by_slot = project_asset_terms_by_slot()
    except ProjectRegistryError as exc:
        # A bad local registry must not cause an unsafe guessed mapping. Keep
        # the known six aliases only, which still fails closed for new names.
        print(f"WARNING: invalid project registry ignored: {exc}", file=sys.stderr)
        terms_by_slot = FIXED_RESUME_PROJECT_ASSET_TERMS
    result: Dict[int, set[str]] = {slot: set() for slot in FIXED_RESUME_PROJECT_ASSET_TERMS}
    for asset in assets:
        title = " ".join(str(asset.get("asset_title") or "").casefold().split())
        for slot, terms in terms_by_slot.items():
            if any(term in title for term in terms):
                result[slot].add(asset["profile_asset_id"])
    return result


def render_fixed_project_asset_rules(project_assets: Mapping[int, set[str]]) -> str:
    """Expose the strict slot-to-asset binding to the resume model."""
    lines = []
    for slot, label in zip(sorted(project_assets), FIXED_RESUME_PROJECTS):
        allowed = sorted(project_assets[slot])
        cited = ", ".join(allowed) if allowed else "NONE — do not use this block"
        lines.append(f"- slots {slot}-{slot + 1} ({label.split(' ', 1)[1]}): {cited}")
    return "\n".join(lines)


# ---------------------------------------------------------------- prompts

GROUNDING_RULES = """
Grounding rules (violating any of these makes the output unusable):
1. Every statement you write must be traceable to exactly one ASSET block below.
2. You must record that asset's id in the "source_asset_id" field for the statement.
3. Do not merge two assets into one statement. One statement, one source.
4. Do not add employers, job titles, dates, certifications, clearances, degrees
   in progress, or metrics that do not appear verbatim in an ASSET block.
5. Honour every MUST NOT CLAIM line. These are hard constraints, not style notes.
6. Academic and course project work must be described as such. Never imply
   professional, production, or employment experience.
7. If the job requires something no asset supports, do not write around it.
   Leave it out and list it in "not_supported".
8. Write plainly. No superlatives, no "passionate", no invented enthusiasm.
"""


def build_resume_prompt(
    app: Dict[str, Any], catalog: str, max_bullets: int,
    project_assets: Optional[Mapping[int, set[str]]] = None,
    baseline_subtitles: Optional[Mapping[int, str]] = None,
    baseline_bullets: Optional[Mapping[int, str]] = None,
    experience_baselines: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> str:
    """Compatibility wrapper around the policy-locked resume prompt template."""
    return build_resume_tailoring_prompt(
        app=app, asset_catalog=catalog, max_project_bullets=max_bullets,
        fixed_projects=FIXED_RESUME_PROJECTS,
        fixed_project_asset_rules=render_fixed_project_asset_rules(
            project_assets or {slot: set() for slot in FIXED_RESUME_PROJECT_ASSET_TERMS}
        ),
        baseline_subtitles=baseline_subtitles or {},
        baseline_project_bullets=baseline_bullets or {},
        experience_baselines=experience_baselines or {},
    )


def normalize_cover_alignment_blueprint(parsed: Mapping[str, Any], jd_text: str) -> Dict[str, Any]:
    """Keep only exact JD quotes and assign stable category-local IDs.

    The extractor is an LLM, so its classification is advisory. Exact-quote
    validation is deterministic and fails closed: invented/paraphrased targets
    never reach the cover-letter generation prompt.
    """
    result: Dict[str, Any] = {}
    specs = (
        ("about_me_targets", "A"), ("interest_targets", "I"),
        ("soft_skill_targets", "S"), ("technical_targets", "T"),
    )
    normalized_jd = " ".join(str(jd_text or "").split()).casefold()
    for key, prefix in specs:
        clean = []
        raw_items = parsed.get(key, []) if isinstance(parsed, Mapping) else []
        if not isinstance(raw_items, list):
            raw_items = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            quote = " ".join(str(item.get("quote") or "").split())
            if len(quote) < 4 or quote.casefold() not in normalized_jd:
                continue
            clean.append({
                "id": f"{prefix}{len(clean) + 1}",
                "quote": quote,
                "why": " ".join(str(item.get("why") or "").split()),
            })
        result[key] = clean
    return result


def merge_cover_alignment_blueprint(
    existing: Mapping[str, Any], additions: Mapping[str, Any], jd_text: str,
) -> Dict[str, Any]:
    """Merge a completeness-audit result and re-normalize exact JD quotes."""
    combined: Dict[str, list[dict[str, Any]]] = {}
    for key in ("about_me_targets", "interest_targets", "soft_skill_targets", "technical_targets"):
        rows: list[dict[str, Any]] = []
        for source in (existing, additions):
            values = source.get(key, []) if isinstance(source, Mapping) else []
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, Mapping):
                    rows.append({
                        "quote": item.get("quote", ""),
                        "why": item.get("why", ""),
                    })
        combined[key] = rows
    return normalize_cover_alignment_blueprint(combined, jd_text)


def build_cover_letter_prompt(
    app: Dict[str, Any], catalog: str, alignment_blueprint: Optional[Mapping[str, Any]] = None,
) -> str:
    """Compatibility wrapper around the sentence-classified cover prompt."""
    return build_cover_letter_tailoring_prompt(
        app=app, asset_catalog=catalog, alignment_blueprint=alignment_blueprint or {},
    )

def build_short_answer_prompt(app: Dict[str, Any], catalog: str, questions: List[str]) -> str:
    return f"""You are JobOS Short Answer Agent V1.

Answer these application form questions for {app['job_title']} at {app['company']},
using ONLY the approved assets below.

QUESTIONS:
{json.dumps(questions, indent=2, ensure_ascii=False)}

{GROUNDING_RULES}
9. If no asset supports an answer, set "answerable": false and explain what is
   missing. A refusal that the user can fill in themselves is correct behaviour.
   Never invent a plausible-sounding answer.

APPROVED ASSETS:
{catalog}

Return ONLY valid JSON:
{{
  "answers": [
    {{
      "question": "verbatim question",
      "answerable": true,
      "text": "the answer, under 120 words",
      "source_asset_id": "<uuid, or \\"none\\" when answerable is false>",
      "missing_information": "what the user must supply, when answerable is false"
    }}
  ],
  "self_check": "one sentence confirming no answer exceeds the evidence"
}}
"""


# ---------------------------------------------------------------- validation

def validate_and_render(
    doc_type: str, parsed: Dict[str, Any], valid_asset_ids: set,
    valid_company_urls: Optional[set] = None,
    fixed_project_assets: Optional[Mapping[int, set[str]]] = None,
    max_resume_bullets: int = 12,
    baseline_subtitles: Optional[Mapping[int, str]] = None,
    baseline_bullets: Optional[Mapping[int, str]] = None,
    jd_text: str = "",
    company: str = "",
    job_title: str = "",
    experience_baselines: Optional[Mapping[int, Mapping[str, Any]]] = None,
    experience_source_asset_ids: Optional[set[str]] = None,
    matched_requirement_ids: Optional[set[str]] = None,
    resume_coverage_target_percent: int = 0,
    resume_total_material_requirement_count: int = 0,
    cover_alignment_blueprint: Optional[Mapping[str, Any]] = None,
    expected_questions: Optional[List[str]] = None,
) -> Tuple[str, List[str], Dict[str, Any], List[str]]:
    """Drop any claim citing an unknown asset. Returns
    (content, asset_ids_used, evidence_map, dropped)."""
    dropped: List[str] = []
    used: List[str] = []
    lines: List[str] = []
    evidence: Dict[str, Any] = {"doc_type": doc_type, "claims": [], "warnings": []}
    valid_company_urls = valid_company_urls or set()
    matched_requirement_ids = set(matched_requirement_ids or set())
    experience_source_asset_ids = (
        set(experience_source_asset_ids) if experience_source_asset_ids is not None else None
    )
    covered_requirement_ids: set[str] = set()

    def mapping_list(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    def string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def requirement_ids(item: Mapping[str, Any]) -> list[str]:
        raw = item.get("matched_requirement_ids") or item.get("alignment_ids") or []
        if not isinstance(raw, list):
            return []
        clean = [str(value).strip() for value in raw if str(value).strip()]
        return [value for value in clean if not matched_requirement_ids or value in matched_requirement_ids]

    def check(src: Optional[str], text: str, allow_none: bool = False) -> bool:
        if allow_none and (src in (None, "", "none")):
            return True
        if src not in valid_asset_ids:
            dropped.append(f"{text[:70]}... (cited unknown asset: {src})")
            return False
        return True

    if doc_type == "resume":
        experience_bullets, project_bullets, skill_lines, project_subtitles = [], [], [], []
        seen_experience_slots: set[int] = set()
        for item in mapping_list(parsed.get("experience_updates")):
            try:
                slot = int(item.get("slot"))
            except (TypeError, ValueError):
                continue
            if slot in seen_experience_slots or slot not in (experience_baselines or {}):
                continue
            text, src = (item.get("text") or "").strip(), item.get("source_asset_id")
            if not text:
                continue
            if src in (None, "", "none"):
                src = None
            elif not check(src, text):
                continue
            change = {
                "slot": slot, "claim": text,
                "header_context": item.get("header_context", ""),
                "previous_bullet": item.get("previous_bullet", ""),
                "jd_requirement_quote": item.get("jd_requirement_quote", ""),
                "experience_evidence_quote": item.get("experience_evidence_quote", ""),
                "word_change_rationale": item.get("word_change_rationale"),
                "why_better": item.get("why_better", ""),
            }
            audit_problems = validate_experience_bullet_change(
                change, baselines=experience_baselines or {}, jd_text=jd_text,
            )
            if audit_problems:
                dropped.append(f"{text[:70]}... (experience bullet audit: {'; '.join(audit_problems)})")
                continue
            req_ids = requirement_ids(item)
            if matched_requirement_ids and not req_ids:
                evidence["warnings"].append(
                    f"Experience slot {slot} is JD-aligned but has no supportable requirement ID; kept it without counting it toward resume coverage."
                )
            seen_experience_slots.add(slot)
            covered_requirement_ids.update(req_ids)
            lines.append(f"- {text}")
            if src:
                used.append(src)
            claim = {
                "claim": text, "source_asset_id": src, "kind": "resume_experience_bullet_change",
                "slot": slot, "header_context": normalize_experience(item.get("header_context")),
                "previous_bullet": normalize_experience(item.get("previous_bullet")),
                "jd_requirement_quote": normalize_experience(item.get("jd_requirement_quote")),
                "experience_evidence_quote": normalize_experience(item.get("experience_evidence_quote")),
                "matched_requirement_ids": req_ids,
                "word_change_rationale": item.get("word_change_rationale"),
                "why_better": normalize_experience(item.get("why_better")),
            }
            evidence["claims"].append(claim)
            experience_bullets.append({"slot": slot, "text": text, **claim})

        raw_bullets = mapping_list(
            parsed.get("project_updates", parsed.get("project_bullets", parsed.get("bullets", [])))
        )
        seen_project_slots: set[int] = set()
        for position, b in enumerate(raw_bullets[:12], start=1):
            if len(project_bullets) >= max(0, min(max_resume_bullets, 12)):
                break
            try:
                slot = int(b.get("slot", position))
            except (TypeError, ValueError):
                continue
            if not 1 <= slot <= 12 or slot in seen_project_slots:
                continue
            if slot % 2 == 0 and slot - 1 not in seen_project_slots:
                continue
            text, src = (b.get("text") or "").strip(), b.get("source_asset_id")
            block_slot = slot if slot % 2 else slot - 1
            if fixed_project_assets is not None and src not in fixed_project_assets.get(block_slot, set()):
                dropped.append(
                    f"{text[:70]}... (asset {src} is not approved for fixed project slot {block_slot}-{block_slot + 1})"
                )
                continue
            limit = 200 if slot % 2 else 105
            if not text or len(text) > limit or not check(src, text):
                continue
            change = {
                "slot": slot, "claim": text, "previous_bullet": b.get("previous_bullet", ""),
                "jd_requirement_quote": b.get("jd_requirement_quote", ""),
                "project_evidence_quote": b.get("project_evidence_quote", ""),
                "word_change_rationale": b.get("word_change_rationale"),
                "why_better": b.get("why_better", ""),
            }
            audit_problems = validate_bullet_change(
                change, baseline_bullets=baseline_bullets or {}, jd_text=jd_text,
            )
            if audit_problems:
                dropped.append(f"{text[:70]}... (project bullet audit: {'; '.join(audit_problems)})")
                continue
            req_ids = requirement_ids(b)
            if matched_requirement_ids and not req_ids:
                dropped.append(f"{text[:70]}... (project rewrite has no valid matched_requirement_ids)")
                continue
            seen_project_slots.add(slot)
            covered_requirement_ids.update(req_ids)
            lines.append(f"- {text}")
            used.append(src)
            claim = {
                "claim": text,
                "source_asset_id": src,
                "kind": "resume_project_bullet_change", "slot": slot,
                "previous_bullet": normalize_bullet(b.get("previous_bullet")),
                "jd_requirement_quote": normalize_bullet(b.get("jd_requirement_quote")),
                "project_evidence_quote": normalize_bullet(b.get("project_evidence_quote")),
                "word_change_rationale": b.get("word_change_rationale"),
                "why_better": normalize_bullet(b.get("why_better")),
                "evidence_boundary": b.get("evidence_boundary", ""),
                "matched_requirement_ids": req_ids,
            }
            evidence["claims"].append(claim)
            project_bullets.append({"slot": slot, "text": text, **claim})
        for item in mapping_list(parsed.get("skill_lines_ranked", parsed.get("skill_lines", [])))[:5]:
            category, values = (item.get("category") or "").strip(), (item.get("items") or "").strip()
            src = item.get("source_asset_id")
            text = f"{category}: {values}"
            if not category or not values or not check(src, text):
                continue
            req_ids = requirement_ids(item)
            jd_quote = normalize_bullet(item.get("jd_requirement_quote"))
            evidence_quote = normalize_bullet(item.get("skill_evidence_quote"))
            if matched_requirement_ids:
                if not req_ids:
                    dropped.append(f"{text[:70]}... (skill line has no valid matched_requirement_ids)")
                    continue
                if len(jd_quote) < 8 or jd_quote.casefold() not in jd_text.casefold():
                    dropped.append(f"{text[:70]}... (skill line lacks an exact JD quote)")
                    continue
                if len(evidence_quote) < 8:
                    dropped.append(f"{text[:70]}... (skill line lacks an exact user-evidence quote)")
                    continue
            covered_requirement_ids.update(req_ids)
            lines.append(f"- {text}")
            used.append(src)
            claim = {
                "claim": text, "source_asset_id": src, "kind": "resume_skill_line",
                "matched_requirement_ids": req_ids, "jd_requirement_quote": jd_quote,
                "skill_evidence_quote": evidence_quote,
            }
            evidence["claims"].append(claim)
            skill_lines.append({
                "category": category, "items": values, "source_asset_id": src,
                "matched_requirement_ids": req_ids, "jd_requirement_quote": jd_quote,
                "skill_evidence_quote": evidence_quote,
            })
        seen_subtitle_slots: set[int] = set()
        for item in parsed.get("project_subtitle_updates", [])[:6]:
            try:
                slot = int(item.get("slot"))
            except (TypeError, ValueError):
                continue
            text, src = (item.get("text") or "").strip(), item.get("source_asset_id")
            block_slot = slot
            if slot not in {1, 3, 5, 7, 9, 11} or slot in seen_subtitle_slots:
                continue
            if fixed_project_assets is not None and src not in fixed_project_assets.get(block_slot, set()):
                dropped.append(f"{text[:70]}... (asset {src} is not approved for project header slot {slot})")
                continue
            if not text or len(text) > 88 or not check(src, text):
                continue
            change = {
                "slot": slot, "claim": text, "previous_subtitle": item.get("previous_subtitle", ""),
                "jd_requirement_quote": item.get("jd_requirement_quote", ""),
                "project_evidence_quote": item.get("project_evidence_quote", ""),
                "word_change_rationale": item.get("word_change_rationale"),
                "why_better": item.get("why_better", ""),
            }
            audit_problems = validate_subtitle_change(
                change, baseline_subtitles=baseline_subtitles or {}, jd_text=jd_text,
            )
            if audit_problems:
                dropped.append(f"{text[:70]}... (project subtitle audit: {'; '.join(audit_problems)})")
                continue
            req_ids = requirement_ids(item)
            if matched_requirement_ids and not req_ids:
                dropped.append(f"{text[:70]}... (project subtitle has no valid matched_requirement_ids)")
                continue
            seen_subtitle_slots.add(slot)
            covered_requirement_ids.update(req_ids)
            used.append(src)
            lines.append(f"- [Project subtitle slot {slot}] {text}")
            claim = {
                "claim": text, "source_asset_id": src, "kind": "resume_project_subtitle_change", "slot": slot,
                "previous_subtitle": normalize_subtitle(item.get("previous_subtitle")),
                "jd_requirement_quote": normalize_subtitle(item.get("jd_requirement_quote")),
                "project_evidence_quote": normalize_subtitle(item.get("project_evidence_quote")),
                "word_change_rationale": item.get("word_change_rationale"),
                "why_better": normalize_subtitle(item.get("why_better")),
                "matched_requirement_ids": req_ids,
            }
            evidence["claims"].append(claim)
            project_subtitles.append({"slot": slot, "text": text, **claim})
        evidence["resume_template"] = {
            "experience_bullets": experience_bullets,
            "project_bullets": project_bullets, "skill_lines": skill_lines,
            "project_subtitles": project_subtitles,
        }
        supportable_count = len(matched_requirement_ids)
        total_material_count = max(int(resume_total_material_requirement_count or 0), supportable_count)
        truthful_ceiling_percent = (
            100.0 if total_material_count == 0
            else round(100.0 * supportable_count / total_material_count, 1)
        )
        overall_coverage_percent = (
            100.0 if total_material_count == 0
            else round(100.0 * len(covered_requirement_ids) / total_material_count, 1)
        )
        required_covered_count = 0
        if resume_coverage_target_percent and total_material_count:
            required_covered_count = min(
                supportable_count,
                math.ceil(resume_coverage_target_percent * total_material_count / 100.0),
            )
        target_met = (
            not resume_coverage_target_percent
            or len(covered_requirement_ids) >= required_covered_count
        )
        evidence["jd_alignment"] = {
            "target_percent": int(resume_coverage_target_percent or 0),
            "total_material_requirement_count": total_material_count,
            "supportable_requirement_ids": sorted(matched_requirement_ids),
            "covered_requirement_ids": sorted(covered_requirement_ids),
            "supportable_requirement_count": supportable_count,
            "covered_requirement_count": len(covered_requirement_ids),
            "required_supportable_covered_count": required_covered_count,
            "truthful_coverage_ceiling_percent": truthful_ceiling_percent,
            "target_reachable_truthfully": (
                truthful_ceiling_percent >= resume_coverage_target_percent
                if resume_coverage_target_percent else True
            ),
            "coverage_percent": overall_coverage_percent,
            "target_met": target_met,
            "gate_passed": True,
            "non_blocking": True,
            "policy": (
                "80% is an optimization target over all material JD requirements, not a hard gate. "
                "Missing information lowers specificity/coverage; safe edits and baseline content survive."
            ),
        }
        if not target_met:
            evidence["warnings"].append(
                f"Resume JD coverage {overall_coverage_percent:.1f}% is below the {resume_coverage_target_percent}% target; "
                f"kept safe content instead of fabricating missing information."
            )
        if not lines and not dropped:
            # Sparse information must not brick the document pipeline. Safety or
            # binding violations still fail closed rather than being hidden by a
            # baseline fallback. The fixed renderer preserves every slot unchanged.
            baseline_note = "Resume template preserved; no safe JD-specific edits were available from current information."
            lines.append(f"- {baseline_note}")
            evidence["claims"].append({
                "claim": baseline_note, "source_asset_id": None,
                "kind": "resume_structure", "purpose": "baseline_preserved",
            })
            evidence["warnings"].append("No safe resume rewrites survived; preserved the existing resume template.")

    elif doc_type == "cover_letter":
        blueprint = dict(cover_alignment_blueprint or {})
        target_by_id: Dict[str, Dict[str, Any]] = {}
        for key in ("about_me_targets", "interest_targets", "soft_skill_targets", "technical_targets"):
            for item in blueprint.get(key, []) or []:
                if isinstance(item, Mapping) and item.get("id"):
                    target_by_id[str(item["id"])] = dict(item)
        available_positioning_ids = {
            target_id for target_id in target_by_id
            if target_id.startswith(("A", "I", "S"))
        }
        selected_positioning_ids: set[str] = set()
        covered_positioning_ids: set[str] = set()
        company_specific_count = 0
        cover_paragraphs = mapping_list(parsed.get("paragraphs"))
        has_sentence_schema = any(
            isinstance(p.get("sentences"), list)
            for p in cover_paragraphs
        )

        if has_sentence_schema:
            allowed_kinds = {
                "about_me_positioning", "role_interest", "soft_skill_positioning",
                "technical_evidence", "company_interest",
            }
            prefix_by_kind = {
                "about_me_positioning": "A", "role_interest": "I",
                "soft_skill_positioning": "S", "technical_evidence": "T",
                "company_interest": "I",
            }
            for paragraph in cover_paragraphs[:4]:
                if not isinstance(paragraph, Mapping):
                    continue
                accepted_sentences: list[str] = []
                for sentence in mapping_list(paragraph.get("sentences"))[:4]:
                    if not isinstance(sentence, Mapping):
                        continue
                    text = " ".join(str(sentence.get("text") or "").split())
                    kind = str(sentence.get("kind") or "").strip()
                    if not text or kind not in allowed_kinds:
                        continue
                    raw_ids = sentence.get("alignment_ids") or []
                    alignment_ids = [str(value).strip() for value in raw_ids if str(value).strip()] if isinstance(raw_ids, list) else []
                    jd_quote = " ".join(str(sentence.get("jd_requirement_quote") or "").split())
                    expected_prefix = prefix_by_kind[kind]
                    valid_ids = [
                        target_id for target_id in alignment_ids
                        if target_id in target_by_id and target_id.startswith(expected_prefix)
                        and normalize_bullet(target_by_id[target_id].get("quote")) == normalize_bullet(jd_quote)
                    ]
                    if alignment_ids and not valid_ids:
                        dropped.append(f"{text[:70]}... (alignment id/quote does not match validated JD blueprint)")
                        continue
                    if kind != "company_interest":
                        if len(jd_quote) < 4 or jd_quote.casefold() not in jd_text.casefold():
                            dropped.append(f"{text[:70]}... (positioning/evidence sentence lacks exact JD quote)")
                            continue
                    src = sentence.get("source_asset_id")
                    candidate_quote = " ".join(str(sentence.get("candidate_evidence_quote") or "").split())
                    requested_urls = sentence.get("company_source_urls") or []
                    company_urls = [url for url in requested_urls if isinstance(url, str)] if isinstance(requested_urls, list) else []
                    invalid_urls = [url for url in company_urls if url not in valid_company_urls]
                    uses_company_context = bool(sentence.get("uses_company_context")) or bool(company_urls)
                    company_insight = " ".join(str(sentence.get("company_insight") or "").split())
                    company_evidence_quote = " ".join(str(sentence.get("company_evidence_quote") or "").split())

                    if kind == "technical_evidence":
                        if not check(src, text):
                            continue
                        if len(candidate_quote) < 8:
                            dropped.append(f"{text[:70]}... (technical sentence lacks exact approved user-evidence quote)")
                            continue
                        if uses_company_context:
                            dropped.append(f"{text[:70]}... (technical sentence may not mix company-context claims)")
                            continue
                        used.append(src)
                        evidence["claims"].append({
                            "claim": text, "source_asset_id": src, "kind": "cover_letter_evidence",
                            "purpose": "technical_evidence", "alignment_ids": valid_ids,
                            "jd_requirement_quote": jd_quote,
                            "candidate_evidence_quote": candidate_quote,
                            "uses_company_context": False, "company_source_urls": [],
                        })
                    elif kind == "company_interest":
                        if src not in (None, "", "none"):
                            dropped.append(f"{text[:70]}... (company-interest sentence must not use a candidate asset as proof of interest)")
                            continue
                        if not company_urls or invalid_urls:
                            dropped.append(f"{text[:70]}... (company-interest sentence lacks known company source URL)")
                            continue
                        if len(company_insight) < 8 or len(company_evidence_quote) < 8:
                            dropped.append(f"{text[:70]}... (company-interest sentence lacks sourced company insight/evidence quote)")
                            continue
                        company_specific_count += 1
                        evidence["claims"].append({
                            "claim": text, "source_asset_id": None, "kind": "cover_letter_company_interest",
                            "purpose": "company_interest", "alignment_ids": valid_ids,
                            "jd_requirement_quote": jd_quote,
                            "uses_company_context": True, "company_source_urls": company_urls,
                            "company_insight": company_insight,
                            "company_evidence_quote": company_evidence_quote,
                        })
                    else:
                        if src not in (None, "", "none") or uses_company_context:
                            dropped.append(f"{text[:70]}... (subjective positioning must remain uncited and separate from factual/company claims)")
                            continue
                        if len(jd_quote) < 4 or jd_quote.casefold() not in jd_text.casefold():
                            dropped.append(f"{text[:70]}... (positioning sentence lacks an exact JD quote)")
                            continue
                        # A/I/S are intentionally selected, not exhaustive. If the
                        # optional blueprint failed to parse, an exact JD quote can
                        # still support safe subjective positioning without an ID.
                        if alignment_ids and not valid_ids:
                            dropped.append(f"{text[:70]}... (positioning alignment id/quote does not match the candidate pool)")
                            continue
                        evidence["claims"].append({
                            "claim": text, "source_asset_id": None, "kind": "cover_letter_positioning",
                            "positioning_kind": kind, "alignment_ids": valid_ids,
                            "jd_requirement_quote": jd_quote,
                            "uses_company_context": False, "company_source_urls": [],
                        })
                        selected_positioning_ids.update(
                            target_id for target_id in valid_ids if target_id.startswith(("A", "I", "S"))
                        )
                    covered_positioning_ids.update(
                        target_id for target_id in valid_ids if target_id.startswith(("A", "I", "S"))
                    )
                    accepted_sentences.append(text)
                if accepted_sentences:
                    lines.append(" ".join(accepted_sentences))

            coverage = 100.0 if not selected_positioning_ids else round(
                100.0 * len(covered_positioning_ids & selected_positioning_ids) / len(selected_positioning_ids), 1
            )
            evidence["cover_alignment"] = {
                "target_percent": COVER_POSITIONING_TARGET_PERCENT,
                "available_positioning_ids": sorted(available_positioning_ids),
                "selected_positioning_ids": sorted(selected_positioning_ids),
                "covered_positioning_ids": sorted(covered_positioning_ids & selected_positioning_ids),
                "coverage_percent": coverage,
                "gate_passed": True,
                "non_blocking": True,
                "selection_policy": "select only a few A/I/S themes that best fit the user background; exhaustive JD A/I/S coverage is not required",
                "technical_policy": "technical/factual candidate claims require approved user assets; A/I/S positioning may be uncited subjective intent/working style",
                "blueprint": blueprint,
            }
            if valid_company_urls and not company_specific_count:
                evidence["warnings"].append(
                    "Company research was available but no safe sourced company-interest sentence was used; continued without it."
                )
        else:
            # Legacy validation path retained for old stored/test payloads. New
            # generation always uses the sentence-classified schema above.
            for p in cover_paragraphs:
                text, src = (p.get("text") or "").strip(), p.get("source_asset_id")
                if not text:
                    continue
                requested_urls = p.get("company_source_urls") or []
                if not isinstance(requested_urls, list):
                    requested_urls = []
                company_urls = [url for url in requested_urls if isinstance(url, str)]
                invalid_urls = [url for url in company_urls if url not in valid_company_urls]
                uses_company_context = bool(p.get("uses_company_context")) or bool(company_urls)
                if invalid_urls:
                    dropped.append(f"{text[:70]}... (cited unknown company URL)")
                    continue
                if not check(src, text):
                    continue
                if uses_company_context and not company_urls:
                    dropped.append(f"{text[:70]}... (company claim has no source URL)")
                    continue
                if uses_company_context and src not in valid_asset_ids:
                    dropped.append(f"{text[:70]}... (company-specific paragraph must cite a candidate asset)")
                    continue
                jd_quote = (p.get("jd_requirement_quote") or "").strip()
                evidence_quote = (p.get("candidate_evidence_quote") or "").strip()
                if src in valid_asset_ids and (len(jd_quote) < 8 or jd_quote.casefold() not in jd_text.casefold()):
                    dropped.append(f"{text[:70]}... (candidate paragraph lacks an exact JD requirement quote)")
                    continue
                if src in valid_asset_ids and len(evidence_quote) < 8:
                    dropped.append(f"{text[:70]}... (candidate paragraph lacks an exact asset evidence quote)")
                    continue
                company_insight = (p.get("company_insight") or "").strip()
                company_evidence_quote = (p.get("company_evidence_quote") or "").strip()
                why_company_fit = (p.get("why_company_fit") or "").strip()
                if uses_company_context and (len(company_insight) < 12 or len(company_evidence_quote) < 8 or len(why_company_fit) < 24):
                    dropped.append(f"{text[:70]}... (company-specific paragraph needs source quote, insight and fit reason)")
                    continue
                lines.append(text)
                used.append(src)
                if uses_company_context:
                    company_specific_count += 1
                evidence["claims"].append({
                    "claim": text, "source_asset_id": src, "kind": "cover_letter_evidence",
                    "purpose": p.get("purpose", ""), "jd_requirement_quote": jd_quote,
                    "candidate_evidence_quote": evidence_quote,
                    "uses_company_context": uses_company_context, "company_source_urls": company_urls,
                    "company_insight": company_insight, "company_evidence_quote": company_evidence_quote,
                    "why_company_fit": why_company_fit,
                })
            if valid_company_urls and not company_specific_count:
                evidence["warnings"].append(
                    "Company research was available but no verified company-specific paragraph survived; continued without it."
                )

        if company.strip() and job_title.strip():
            opening = f"I am applying for the {job_title.strip()} position at {company.strip()}."
            closing = "Thank you for considering my application."
            if not lines:
                fallback = "I would welcome the opportunity to contribute in this role and learn more about the team's priorities."
                lines = [fallback]
                evidence["claims"].append({
                    "claim": fallback, "source_asset_id": None,
                    "kind": "cover_letter_structure", "purpose": "sparse_information_fallback",
                })
                evidence["warnings"].append(
                    "Cover-letter information was sparse; emitted a conservative subjective fallback instead of failing."
                )
            lines = [opening, *lines, closing]
            evidence["claims"] = [
                {"claim": opening, "source_asset_id": None,
                 "kind": "cover_letter_structure", "purpose": "opening"},
                *evidence["claims"],
                {"claim": closing, "source_asset_id": None,
                 "kind": "cover_letter_structure", "purpose": "closing"},
            ]

    elif doc_type == "short_answers":
        expected = [" ".join(str(q).split()) for q in (expected_questions or []) if str(q).strip()]
        expected_by_key = {q.casefold(): q for q in expected}
        accepted_by_key: Dict[str, Mapping[str, Any]] = {}
        for answer in mapping_list(parsed.get("answers")):
            model_q = " ".join(str(answer.get("question") or "").split())
            if not model_q:
                continue
            key = model_q.casefold()
            if expected_by_key and key not in expected_by_key:
                dropped.append(f"{model_q[:70]}... (short-answer question is not an exact requested question)")
                continue
            if key in accepted_by_key:
                continue
            accepted_by_key[key] = answer

        question_order = expected if expected else [
            " ".join(str(a.get("question") or "").split())
            for a in mapping_list(parsed.get("answers"))
            if str(a.get("question") or "").strip()
        ]
        seen_q: set[str] = set()
        for q in question_order:
            key = q.casefold()
            if key in seen_q:
                continue
            seen_q.add(key)
            answer = accepted_by_key.get(key)
            if answer is None:
                lines.append(f"### {q}\n\n[NEEDS USER INPUT] Not enough reliable information was available to answer safely.")
                evidence["claims"].append({
                    "claim": q, "source_asset_id": None, "answerable": False,
                    "missing_information": "Not enough reliable information was available to answer safely.",
                })
                continue
            if not bool(answer.get("answerable", False)):
                missing = str(answer.get("missing_information") or "").strip() or "Additional user information is required."
                lines.append(f"### {q}\n\n[NEEDS USER INPUT] {missing}")
                evidence["claims"].append({
                    "claim": q, "source_asset_id": None, "answerable": False,
                    "missing_information": missing,
                })
                continue
            text, src = str(answer.get("text") or "").strip(), answer.get("source_asset_id")
            if not text or not check(src, text):
                # A model claimed answerable but failed grounding. Preserve the
                # exact user question and degrade to user input instead of
                # silently dropping it from the document.
                lines.append(f"### {q}\n\n[NEEDS USER INPUT] A grounded answer could not be produced from approved data.")
                evidence["claims"].append({
                    "claim": q, "source_asset_id": None, "answerable": False,
                    "missing_information": "A grounded answer could not be produced from approved data.",
                })
                continue
            lines.append(f"### {q}\n\n{text}")
            used.append(src)
            evidence["claims"].append({
                "claim": text, "source_asset_id": src, "answerable": True, "question": q,
            })

    evidence["not_supported"] = string_list(parsed.get("not_supported"))
    evidence["model_self_check"] = str(parsed.get("self_check") or "")
    evidence["dropped_ungrounded_claims"] = dropped

    separator = "\n" if doc_type == "resume" else "\n\n"
    return separator.join(lines), sorted(set(used)), evidence, dropped


# ---------------------------------------------------------------- persistence

def next_version(cur, application_id: str, doc_type: str) -> int:
    # Different manifests (for example a human revision arriving while a
    # fresh-generation run is finishing) may legitimately produce different
    # documents, but they must never race MAX(version)+1.  This transaction
    # lock is narrow to one application/document lane and releases on commit.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (f"jobos-document:{application_id}:{doc_type}",))
    cur.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM generated_documents "
        "WHERE application_id = %s AND doc_type = %s;",
        (application_id, doc_type),
    )
    return int(cur.fetchone()[0])


def insert_component_run(
    cur, *, component: str, application_id: str, model: str,
    input_json: Dict[str, Any], output_json: Dict[str, Any],
    raw_output: str, prompt: str,
) -> str:
    cur.execute(
        """
        INSERT INTO component_runs (
          component_name, task_type, application_id,
          input_json, output_json, output_text,
          status, model_provider, model_name,
          input_tokens, output_tokens, created_at, finished_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'completed', 'ollama', %s, %s, %s, now(), now())
        RETURNING id::text;
        """,
        (
            component, "document_generation", application_id,
            Jsonb(input_json), Jsonb(output_json), raw_output, model,
            estimate_tokens(prompt), estimate_tokens(raw_output),
        ),
    )
    return str(cur.fetchone()[0])


def insert_document(
    cur, *, application_id: str, doc_type: str, content: str,
    asset_ids: List[str], evidence_map: Dict[str, Any],
    model: str, role_family: Optional[str], version: int,
) -> str:
    cur.execute(
        """
        INSERT INTO generated_documents (
          application_id, doc_type, version, content, format,
          asset_ids_used, evidence_map,
          generator_version, generator_model, target_role_family,
          source_jd_hash, qa_status, approved, created_at
        )
        SELECT %s, %s, %s, %s, 'markdown', %s, %s, %s, %s, %s, jd_hash, NULL, false, now()
          FROM applications WHERE id = %s
        RETURNING id::text;
        """,
        (
            application_id, doc_type, version, content,
            Jsonb(asset_ids), Jsonb(evidence_map),
            GENERATOR_VERSION, model, role_family, application_id,
        ),
    )
    return str(cur.fetchone()[0])


# ---------------------------------------------------------------- freshness preflight

def run_live_project_freshness(*, max_stale_hours: int) -> tuple[bool, str]:
    """Poll current configured GitHub HEADs before a resume can be generated.

    Daily watching is only a convenience.  This live gate is the correctness
    invariant that prevents an old approved project asset from surviving a new
    commit unnoticed.
    """
    script = Path(__file__).resolve().parents[1] / "repo-audit" / "repository_freshness_v1.py"
    result = DEFAULT_PROCESS_RUNNER.run(
        [sys.executable, str(script), "pre-resume", "--max-stale-hours", str(max_stale_hours)],
        cwd=Path(__file__).resolve().parents[2], timeout_s=600,
    )
    detail = (result.output + (f"\n{result.start_error}" if result.start_error else "")).strip()[-4000:]
    return result.ok, detail


def database_resume_freshness(cur, *, allow_last_known_good_hours: int | None = None) -> tuple[bool, dict[str, Any], list[str]]:
    from services.common.profile_freshness import assess_resume_profile, explain_blockers
    report = assess_resume_profile(cur, allow_last_known_good_hours=allow_last_known_good_hours)
    return bool(report.get("resume_profile_ready")), report, explain_blockers(report)


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--application-id", required=True)
    p.add_argument("--doc-type", required=True, choices=DOC_TYPES)
    p.add_argument("--question", action="append", default=[],
                   help="For short_answers. Repeatable.")
    p.add_argument("--max-bullets", type=int, default=8)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--ctx", type=int, default=8192)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--print-prompt", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Generate even when fit_decision is reject.")
    p.add_argument("--revision-source-document-id",
                   help="Exact reviewed resume/cover-letter document being revised from human feedback.")
    p.add_argument("--revision-feedback",
                   help="Candidate-authored revision request. This is an editing instruction, never factual evidence.")
    p.add_argument("--revision-feedback-stdin", action="store_true",
                   help="Read candidate revision feedback from stdin so private text is not exposed in the process command line.")
    p.add_argument("--skip-live-project-refresh", action="store_true",
                   help="Offline diagnostic only. DB freshness gates still apply; current GitHub HEAD is not polled.")
    p.add_argument("--project-max-stale-hours", type=int,
                   default=int(os.getenv("JOBOS_PROJECT_MAX_STALE_HOURS", "24")),
                   help="Last-known-good GitHub snapshot age allowed only when GitHub is temporarily unavailable.")
    args = p.parse_args()

    if args.doc_type == "short_answers" and not args.question:
        print("ERROR: --doc-type short_answers requires at least one --question.")
        return 2

    if args.doc_type == "resume":
        if args.skip_live_project_refresh:
            print("WARNING: --skip-live-project-refresh disables the live GitHub HEAD poll; use only for offline diagnostics.")
        else:
            ok, detail = run_live_project_freshness(max_stale_hours=args.project_max_stale_hours)
            if not ok:
                print("ERROR: live project freshness preflight failed. Resume generation is blocked.")
                if detail:
                    print(detail)
                return 2

    print("===== DOCUMENT GENERATOR V1 =====")
    print(f"Generator: {GENERATOR_VERSION}")
    print(f"Doc type:  {args.doc_type}")
    print(f"Mode:      {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Model:     {args.model}\n")

    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            if args.doc_type == "resume":
                ready, freshness_report, blockers = database_resume_freshness(
                    cur,
                    allow_last_known_good_hours=(None if args.skip_live_project_refresh else args.project_max_stale_hours),
                )
                if not ready:
                    print("ERROR: resume profile freshness gate is blocked.")
                    for blocker in blockers:
                        print(f"  - {blocker}")
                    print(json.dumps({"freshness": freshness_report}, indent=2, default=str))
                    return 2
            try:
                app = fetch_application_context(cur, args.application_id)
            except RuntimeError as e:
                if "reject" in str(e) and args.force:
                    cur.execute(
                        "SELECT company, job_title FROM applications WHERE id = %s;",
                        (args.application_id,),
                    )
                    r = cur.fetchone()
                    app = {
                        "id": args.application_id, "company": r[0], "job_title": r[1],
                        "jd_text": "", "fit_score": 0, "fit_decision": "reject_forced",
                        "role_family": None, "seniority_level": "",
                        "matched_requirements": [], "missing_or_weak_requirements": [],
                        "hard_blockers": [], "risk_flags": [],
                        "company_context": {},
                    }
                    print("WARNING: generating against a rejected application (--force).\n")
                else:
                    print(f"ERROR: {e}")
                    return 1

            revision_context: dict[str, Any] = {}
            if args.revision_feedback and args.revision_feedback_stdin:
                print("ERROR: use only one of --revision-feedback or --revision-feedback-stdin.")
                return 2
            feedback = ((sys.stdin.read() if args.revision_feedback_stdin else (args.revision_feedback or ""))).strip()
            if feedback or args.revision_source_document_id:
                if args.doc_type not in {"resume", "cover_letter"} or not feedback or not args.revision_source_document_id:
                    print("ERROR: document revisions require both --revision-source-document-id and --revision-feedback for resume/cover_letter.")
                    return 2
                cur.execute(
                    """SELECT gd.doc_type,gd.content,gd.evidence_map,gd.source_jd_hash,a.jd_hash
                         FROM generated_documents gd JOIN applications a ON a.id=gd.application_id
                        WHERE gd.id=%s AND gd.application_id=%s;""",
                    (args.revision_source_document_id, args.application_id),
                )
                prior = cur.fetchone()
                if not prior or str(prior[0]) != args.doc_type:
                    print("ERROR: revision source document is not the exact same application/document type.")
                    return 2
                if not prior[3] or str(prior[3]) != str(prior[4] or ""):
                    print("ERROR: revision source document is stale against the current JD; regenerate from the current application context instead.")
                    return 2
                revision_context = {
                    "source_document_id": str(args.revision_source_document_id),
                    "current_content": str(prior[1] or ""),
                    "current_resume_template": dict((prior[2] or {}).get("resume_template") or {}),
                    "human_feedback": feedback,
                }

            assets = fetch_source_assets(cur, app["role_family"])
            if not assets:
                print("WARNING: no approved profile assets; generating only safe JD/role positioning or baseline-preserving output.")

            valid_ids = {a["profile_asset_id"] for a in assets}
            resume_experience_asset_ids = {
                a["profile_asset_id"] for a in assets if a.get("asset_type") == "source_document_asset"
            }
            resume_project_assets = fixed_project_asset_ids(assets)
            resume_subtitle_baselines: dict[int, str] = {}
            resume_bullet_baselines: dict[int, str] = {}
            resume_experience_baselines: dict[int, dict[str, Any]] = {}
            if args.doc_type == "resume":
                try:
                    resume_subtitle_baselines = load_template_subtitle_baselines()
                    resume_bullet_baselines = load_template_bullet_baselines()
                    resume_experience_baselines = load_template_experience_baselines()
                except (ResumeHeaderAuditError, ResumeBulletAuditError, ResumeExperienceAuditError) as exc:
                    print(f"ERROR: resume auditing needs the fixed Word template: {exc}")
                    return 1
            valid_company_urls = set((app.get("company_context") or {}).get("sources") or [])

            field = {
                "resume": "resume_bullet_bank",
                "cover_letter": "cover_letter_positioning",
                "short_answers": "job_oriented_summary",
            }[args.doc_type]
            catalog = render_asset_catalog(assets, field=field)

            attempt_id: str | None = None
            if args.apply:
                # This is intentionally before *every* model call.  A cover
                # letter performs alignment calls before its final draft; a
                # fence below those calls would still permit duplicate work
                # after a crash.
                # Bind durable idempotency to the resolved model transport as well as
                # prompt-bearing inputs.  API keys are deliberately excluded.
                resolved_llm = resolve_config(
                    role="docgen", model=args.model, local_url=args.ollama_url,
                )
                manifest = {
                    "application_id": app["id"], "doc_type": args.doc_type,
                    "request_kind": "revision" if revision_context else "generation",
                    "jd_sha256": hashlib.sha256(app["jd_text"].encode("utf-8")).hexdigest(),
                    "asset_ids": sorted(valid_ids),
                    # IDs alone do not identify a mutable approved asset.
                    # Hash its exact source snapshot and all prompt-bearing
                    # company/template inputs so changed evidence creates a
                    # new durable generation identity.
                    "asset_snapshot_sha256": hashlib.sha256(json.dumps(
                        assets, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
                    ).encode("utf-8")).hexdigest(),
                    "company_context_sha256": hashlib.sha256(json.dumps(
                        app.get("company_context") or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
                    ).encode("utf-8")).hexdigest(),
                    "resume_template_sha256": hashlib.sha256(json.dumps(
                        {"subtitles": resume_subtitle_baselines, "bullets": resume_bullet_baselines,
                         "experience": resume_experience_baselines},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
                    ).encode("utf-8")).hexdigest(),
                    "generator_version": GENERATOR_VERSION,
                    "llm_backend": resolved_llm.backend,
                    "llm_provider": resolved_llm.provider,
                    "llm_api_style": resolved_llm.api_style,
                    "model": resolved_llm.model, "max_bullets": args.max_bullets,
                    "questions": list(args.question),
                    "revision_source_document_id": revision_context.get("source_document_id"),
                    "feedback_sha256": hashlib.sha256(
                        str(revision_context.get("human_feedback") or "").encode("utf-8")
                    ).hexdigest() if revision_context else None,
                }
                try:
                    attempt = claim_document_attempt(
                        cur, application_id=app["id"], doc_type=args.doc_type,
                        request_kind=str(manifest["request_kind"]), input_manifest=manifest,
                        # Cover-letter generation can make multiple model calls.  The
                        # lease therefore covers the configured per-call timeout plus
                        # a bounded orchestration margin; stale leases are recoverable.
                        lease_seconds=max(300, int(args.timeout) * 4 + 120),
                    )
                except DocumentAttemptError as exc:
                    print(f"ERROR: {exc}")
                    conn.rollback()
                    return 1
                if attempt.completed_document_id:
                    conn.commit()
                    print("\n===== REUSED DURABLE GENERATION =====")
                    print(f"generated_document_id: {attempt.completed_document_id}")
                    return 0
                attempt_id = attempt.id
                conn.commit()

            cover_alignment_blueprint: dict[str, Any] = {}
            alignment_prompt = ""
            alignment_raw = ""
            alignment_audit_prompt = ""
            alignment_audit_raw = ""
            if args.doc_type == "resume":
                prompt = build_resume_prompt(
                    app, catalog, args.max_bullets, resume_project_assets, resume_subtitle_baselines,
                    resume_bullet_baselines, resume_experience_baselines,
                )
            elif args.doc_type == "cover_letter":
                alignment_prompt = build_cover_alignment_blueprint_prompt(app=app)
                try:
                    alignment_raw = ollama_generate(
                        model=args.model, prompt=alignment_prompt, ollama_url=args.ollama_url,
                        timeout=args.timeout, temperature=0.0, num_ctx=args.ctx,
                    )
                    cover_alignment_blueprint = normalize_cover_alignment_blueprint(
                        extract_json_object(alignment_raw), app["jd_text"]
                    )
                    alignment_audit_prompt = build_cover_alignment_audit_prompt(
                        app=app, alignment_blueprint=cover_alignment_blueprint
                    )
                    alignment_audit_raw = ollama_generate(
                        model=args.model, prompt=alignment_audit_prompt, ollama_url=args.ollama_url,
                        timeout=args.timeout, temperature=0.0, num_ctx=args.ctx,
                    )
                    cover_alignment_blueprint = merge_cover_alignment_blueprint(
                        cover_alignment_blueprint, extract_json_object(alignment_audit_raw), app["jd_text"]
                    )
                except (ValueError, json.JSONDecodeError) as exc:
                    print(f"WARNING: cover-letter alignment candidate extraction was not parseable: {exc}; continuing without a blueprint.")
                    cover_alignment_blueprint = {}
                except Exception as exc:
                    if attempt_id:
                        fail_document_attempt(cur, attempt_id=attempt_id, error=str(exc), uncertain=True)
                        conn.commit()
                    raise
                prompt = build_cover_letter_prompt(app, catalog, cover_alignment_blueprint)
            else:
                prompt = build_short_answer_prompt(app, catalog, args.question)

            if revision_context:
                prompt += (
                    "\n\nHUMAN REVISION REQUEST — EDITING DIRECTION ONLY, NEVER EVIDENCE\n"
                    "The candidate reviewed the previous draft and requested a targeted revision.\n"
                    "Follow the request where it is compatible with every existing grounding, immutable-field, JD, and do-not-overclaim rule.\n"
                    "Do NOT treat the feedback text itself as proof of a fact, metric, tool, title, employer, date, project outcome, or skill.\n"
                    "Preserve verified parts of the current draft that the request does not ask to change.\n"
                    "If the requested change cannot be supported, keep the truthful baseline/current wording and report it in not_supported/self_check instead of inventing support.\n\n"
                    "CURRENT REVIEWED DRAFT:\n" + revision_context["current_content"] +
                    "\n\nCURRENT STRUCTURED RESUME OVERRIDES (if any):\n" +
                    json.dumps(revision_context.get("current_resume_template") or {}, ensure_ascii=False, sort_keys=True) +
                    "\n\nCANDIDATE FEEDBACK:\n" + revision_context["human_feedback"] + "\n"
                )

            print(f"Company:        {app['company']}")
            print(f"Job title:      {app['job_title']}")
            print(f"Fit:            {app['fit_score']} / {app['fit_decision']}")
            print(f"Approved assets:{len(assets)}")
            print(f"Prompt tokens~: {estimate_tokens(prompt)}\n")

            if args.print_prompt:
                print("===== PROMPT =====")
                print(prompt)
                print("===== END PROMPT =====\n")

            start = time.perf_counter()
            try:
                raw = ollama_generate(
                    model=args.model, prompt=prompt, ollama_url=args.ollama_url,
                    timeout=args.timeout, temperature=args.temperature, num_ctx=args.ctx,
                )
            except Exception as exc:
                if args.apply and attempt_id:
                    fail_document_attempt(cur, attempt_id=attempt_id, error=str(exc), uncertain=True)
                    conn.commit()
                raise
            elapsed = time.perf_counter() - start
            emit_trace(
                make_trace_id("docgen", app["id"], args.doc_type),
                "document_generation",
                started_at=start,
                tokens_in=estimate_tokens(prompt),
                tokens_out=estimate_tokens(raw),
                cost_usd=0.0,
                application_id=app["id"],
                doc_type=args.doc_type,
            )

            try:
                parsed = extract_json_object(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                print(f"WARNING: generated JSON was not parseable: {exc}; using a conservative fallback payload.")
                if args.doc_type == "short_answers":
                    parsed = {"answers": [
                        {"question": q, "answerable": False, "text": "", "source_asset_id": "none",
                         "missing_information": "Not enough reliable information was available to answer safely."}
                        for q in args.question
                    ], "not_supported": [], "self_check": "fallback: needs user input"}
                else:
                    parsed = {"experience_updates": [], "project_updates": [], "skill_lines_ranked": [],
                              "project_subtitle_updates": [], "paragraphs": [], "not_supported": [],
                              "self_check": "fallback: preserved safe baseline/minimal positioning"}
            resume_coverage = material_requirement_summary(app) if args.doc_type == "resume" else {
                "supportable": [], "total_material_requirements": 0
            }
            resume_requirement_ids = {
                item["id"] for item in resume_coverage["supportable"]
            } if args.doc_type == "resume" else set()
            content, used, evidence, dropped = validate_and_render(
                args.doc_type, parsed, valid_ids, valid_company_urls,
                resume_project_assets if args.doc_type == "resume" else None,
                args.max_bullets if args.doc_type == "resume" else 12,
                resume_subtitle_baselines if args.doc_type == "resume" else None,
                resume_bullet_baselines if args.doc_type == "resume" else None,
                app["jd_text"],
                app["company"] if args.doc_type == "cover_letter" else "",
                app["job_title"] if args.doc_type == "cover_letter" else "",
                experience_baselines=resume_experience_baselines if args.doc_type == "resume" else None,
                experience_source_asset_ids=(resume_experience_asset_ids if args.doc_type == "resume" else None),
                matched_requirement_ids=resume_requirement_ids,
                resume_coverage_target_percent=(RESUME_TARGET_COVERAGE_PERCENT if args.doc_type == "resume" else 0),
                resume_total_material_requirement_count=(
                    resume_coverage["total_material_requirements"] if args.doc_type == "resume" else 0
                ),
                cover_alignment_blueprint=cover_alignment_blueprint if args.doc_type == "cover_letter" else None,
                expected_questions=args.question if args.doc_type == "short_answers" else None,
            )

            print("===== GENERATED =====")
            print(content or "(empty -- every claim was ungrounded and dropped)")
            print(f"\nElapsed:            {elapsed:.1f}s")
            print(f"Assets cited:       {len(used)}")
            print(f"Ungrounded dropped: {len(dropped)}")
            for d in dropped:
                print(f"  DROPPED: {d}")
            if evidence["not_supported"]:
                print("\nJD requirements left unaddressed (correctly):")
                for n in evidence["not_supported"]:
                    print(f"  - {n}")

            if not content.strip():
                print("\nNothing grounded survived validation. Not saving.")
                if args.apply and attempt_id:
                    fail_document_attempt(
                        cur, attempt_id=attempt_id,
                        error="No grounded document content survived deterministic validation.",
                    )
                    conn.commit()
                return 1

            if not args.apply:
                conn.rollback()
                print("\nDRY RUN ONLY. No database changes committed.")
                return 0

            version = next_version(cur, app["id"], args.doc_type)
            doc_id = insert_document(
                cur, application_id=app["id"], doc_type=args.doc_type,
                content=content, asset_ids=used, evidence_map=evidence,
                model=args.model, role_family=app["role_family"], version=version,
            )
            if attempt_id:
                complete_document_attempt(cur, attempt_id=attempt_id, document_id=doc_id)
            insert_component_run(
                cur,
                component=COMPONENT_BY_DOC_TYPE[args.doc_type],
                application_id=app["id"], model=args.model,
                input_json={
                    "doc_type": args.doc_type,
                    "generator_version": GENERATOR_VERSION,
                    "approved_asset_count": len(assets),
                    "company_context_source_count": len(valid_company_urls),
                    "questions": args.question,
                    "revision_source_document_id": revision_context.get("source_document_id") if revision_context else None,
                    "human_revision_feedback": revision_context.get("human_feedback") if revision_context else None,
                    "resume_target_coverage_percent": (RESUME_TARGET_COVERAGE_PERCENT if args.doc_type == "resume" else None),
                    "cover_positioning_target_percent": (COVER_POSITIONING_TARGET_PERCENT if args.doc_type == "cover_letter" else None),
                    "cover_alignment_blueprint": cover_alignment_blueprint,
                    "alignment_prompt_tokens": estimate_tokens(alignment_prompt) if alignment_prompt else 0,
                    "alignment_output_tokens": estimate_tokens(alignment_raw) if alignment_raw else 0,
                    "alignment_audit_prompt_tokens": estimate_tokens(alignment_audit_prompt) if alignment_audit_prompt else 0,
                    "alignment_audit_output_tokens": estimate_tokens(alignment_audit_raw) if alignment_audit_raw else 0,
                },
                output_json=evidence, raw_output=raw, prompt=prompt,
            )
            conn.commit()

            print("\n===== SAVED =====")
            print(f"generated_document_id: {doc_id}")
            print(f"version:               {version}")
            print(f"qa_status:             NULL (queued for truth checker)")
            print("\nNext: verify_document_truth_v1.py --document-id " + doc_id)
            return 0


if __name__ == "__main__":
    sys.exit(main())
