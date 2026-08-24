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
from services.common.llm_gateway import generate_text
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
from services.common.config import database_dsn

DSN = database_dsn()

GENERATOR_VERSION = "document_generator_v2_structural_cover_grounding_2026_08_23"
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
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in model output.")
    return json.loads(cleaned[first:last + 1])


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
    sources = sorted({
        value.strip() for value in (row[5] or [])
        if isinstance(value, str) and value.strip().startswith(("https://", "http://"))
    })
    if not sources:
        return {}
    return {
        "company_domain": row[0] or "",
        "summary": row[1] or "",
        "mission": row[2] or "",
        "products": row[3] or "",
        "recent_news": row[4] or [],
        "sources": sources,
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
) -> str:
    return f"""You are JobOS Resume Agent V1.

Write resume bullets for this specific job, using ONLY the approved assets below.

TARGET ROLE: {app['job_title']} at {app['company']}
ROLE FAMILY: {app['role_family']}
SENIORITY: {app['seniority_level']}

REQUIREMENTS THE FIT ANALYSIS MATCHED:
{json.dumps(app['matched_requirements'], indent=2, ensure_ascii=False)}

KNOWN GAPS (do not paper over these):
{json.dumps(app['missing_or_weak_requirements'], indent=2, ensure_ascii=False)}

{GROUNDING_RULES}

APPROVED ASSETS:
{catalog}

Return ONLY valid JSON, no markdown, no commentary:
{{
  "project_updates": [
    {{
      "slot": "integer 1..12 from the fixed slot map below",
      "text": "one project resume bullet, max 30 words, starts with a past-tense verb",
      "source_asset_id": "<uuid copied exactly from an ASSET block>",
      "previous_bullet": "exact current template bullet text for this slot, copied from BULLET BASELINE below",
      "jd_requirement_quote": "exact verbatim phrase from the JD that this bullet addresses",
      "project_evidence_quote": "exact verbatim phrase from the cited ASSET that supports the new wording",
      "word_change_rationale": [
        {{"before": "old/removed word or phrase", "after": "replacement word or phrase", "why": "specific JD + project-evidence reason"}}
      ],
      "why_better": "specific reason this bullet is more accurate and more relevant than previous_bullet",
      "evidence_boundary": "academic project | coursework | research | lab exercise"
    }}
  ],
  "skill_lines_ranked": [
    {{
      "category": "one existing resume skill category, max 45 characters",
      "items": "comma-separated skills, max 220 characters, all backed by the cited asset",
      "source_asset_id": "<uuid copied exactly from an ASSET block>"
    }}
  ],
  "project_subtitle_updates": [
    {{
      "slot": "one of 1, 3, 5, 7, 9, 11; identifies the header that contains this project name",
      "text": "replacement subtitle only: text between the fixed project name and the fixed GitHub link, max 88 chars",
      "source_asset_id": "<uuid copied exactly from the matching project ASSET>",
      "previous_subtitle": "exact template subtitle copied from BASELINE below",
      "jd_requirement_quote": "exact verbatim phrase from the JD that this subtitle addresses",
      "project_evidence_quote": "exact verbatim phrase from the cited ASSET that supports the subtitle",
      "word_change_rationale": [
        {{"before": "old/removed word or phrase", "after": "replacement word or phrase", "why": "specific JD + project-evidence reason"}}
      ],
      "why_better": "specific reason this subtitle is more accurate and more relevant than previous_subtitle"
    }}
  ],
  "not_supported": ["JD requirements no asset can back"],
  "self_check": "one sentence: confirm no bullet claims professional experience"
}}

Fixed project slots (title/date/link are immutable): {', '.join(FIXED_RESUME_PROJECTS)}.
CURRENT FIXED-TEMPLATE SUBTITLE BASELINE (these are the only permitted `previous_subtitle` values):
{json.dumps(dict(baseline_subtitles or {}), indent=2, ensure_ascii=False)}
CURRENT FIXED-TEMPLATE BULLET BASELINE (these are the only permitted `previous_bullet` values):
{json.dumps(dict(baseline_bullets or {}), indent=2, ensure_ascii=False)}
Allowed source asset IDs for each immutable project block (cite only the IDs
listed for the slot you update; `NONE` means that block is unavailable):
{render_fixed_project_asset_rules(project_assets or {slot: set() for slot in FIXED_RESUME_PROJECT_ASSET_TERMS})}
Select only tightly JD-relevant project slots backed by their asset. Do not
invent a new project or reuse a weakly related one. For each selected project,
use at most its two slots: odd primary slots are max 200 characters (target two
visual lines); even secondary slots are max 105 characters (target one visual
line). Do not output a secondary slot without its matching primary slot.
Use full lines when backed by evidence, but rewrite if the final wrapped line
would contain only a few words. Rank skills by explicit JD relevance first,
then profile evidence, while retaining diverse categories; omit weak/unproven
skills. Do not write education, employment, certification, contact,
company-motivation, project name, project date, GitHub URL/link, or generic
summary text: those fields are preserved verbatim from the template and this
model has no control over them.
Only `project_subtitle_updates.text` may change the words between the project
name and GitHub link. Every changed substantive subtitle word must appear in
`word_change_rationale`; no cosmetic or generic keyword edits.
Every project bullet rewrite has the same evidence requirement: cite one exact
JD phrase, one exact phrase from the matching project asset, and explain each
substantive changed word. If the project does not genuinely match the JD, do
not output a bullet for it. Never introduce a tool, result, responsibility, or
experience that is not in the approved profile asset.
Produce at most {min(max_bullets, 12)} project bullets. Fewer strong grounded
bullets are better than weak filler; any unselected template bullet is cleared.
"""


def build_cover_letter_prompt(app: Dict[str, Any], catalog: str) -> str:
    company_context = app.get("company_context") or {}
    return f"""You are JobOS Cover Letter Agent V1.

Write a short cover letter using approved candidate assets and the separately
sourced company context below.

TARGET ROLE: {app['job_title']} at {app['company']}
FIT SCORE: {app['fit_score']} ({app['fit_decision']})

MATCHED JD REQUIREMENTS:
{json.dumps(app.get('matched_requirements', []), indent=2, ensure_ascii=False)}

KNOWN GAPS (do not hide these with invented experience):
{json.dumps(app.get('missing_or_weak_requirements', []), indent=2, ensure_ascii=False)}

RISK FLAGS RAISED BY THE FIT ANALYSIS (address honestly or stay silent, never contradict):
{json.dumps(app['risk_flags'], indent=2, ensure_ascii=False)}

{GROUNDING_RULES}
9. State the candidate's actual level plainly. If the evidence is academic,
   the letter must read as a capable new graduate, not a seasoned practitioner.
10. Company facts may be used only from the SOURCED COMPANY CONTEXT below.
    For every paragraph that uses a company fact, set "uses_company_context"
    true and copy one or more matching URLs exactly into "company_source_urls".
11. Company facts are not candidate evidence: they never replace a real
    source_asset_id for a paragraph about the candidate.
12. If the company context is empty, do not claim familiarity beyond the JD.
13. Spend effort on relevance, not invention: for every candidate-evidence
    paragraph cite one exact JD phrase and one exact phrase from its asset.
14. When company context is present, include at least one genuinely
    company-specific paragraph with a cited source URL, a concrete sourced
    insight, and an honest explanation of how an approved candidate asset
    connects to it. That paragraph must cite a real candidate asset. Do not
    invent a company initiative, culture, product, or experience merely to
    sound tailored.

SOURCED COMPANY CONTEXT (may be empty):
{json.dumps(company_context, indent=2, ensure_ascii=False)}

APPROVED ASSETS:
{catalog}

Return ONLY valid JSON:
{{
  "paragraphs": [
    {{
      "text": "one paragraph, 2-4 sentences",
      "source_asset_id": "<uuid copied exactly from an ASSET block>",
      "purpose": "evidence | motivation",
      "jd_requirement_quote": "exact JD phrase for this paragraph",
      "candidate_evidence_quote": "exact phrase from cited ASSET",
      "uses_company_context": false,
      "company_source_urls": ["<exact URL from SOURCED COMPANY CONTEXT, or omit when unused>"],
      "company_insight": "specific source-backed company fact, empty when unused",
      "company_evidence_quote": "exact phrase from SOURCED COMPANY CONTEXT supporting company_insight, empty when unused",
      "why_company_fit": "honest explanation linking company insight, JD and one approved candidate asset, empty when unused"
    }}
  ],
  "not_supported": ["claims deliberately left out"],
  "self_check": "one sentence confirming no unsupported experience is implied"
}}

Write only the substantive evidence/motivation paragraphs (two to four total).
The renderer adds the deterministic greeting, application sentence, and closing;
do not emit an opening or closing paragraph. Every paragraph must cite a real
candidate asset id.
Write a strong, specific application narrative, but never turn academic/project
work into employment or manufacture a company insight. A less flashy letter
with literal evidence is better than a polished false claim.
"""


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
) -> Tuple[str, List[str], Dict[str, Any], List[str]]:
    """Drop any claim citing an unknown asset. Returns
    (content, asset_ids_used, evidence_map, dropped)."""
    dropped: List[str] = []
    used: List[str] = []
    lines: List[str] = []
    evidence: Dict[str, Any] = {"doc_type": doc_type, "claims": []}
    valid_company_urls = valid_company_urls or set()

    def check(src: Optional[str], text: str, allow_none: bool = False) -> bool:
        if allow_none and (src in (None, "", "none")):
            return True
        if src not in valid_asset_ids:
            dropped.append(f"{text[:70]}... (cited unknown asset: {src})")
            return False
        return True

    if doc_type == "resume":
        project_bullets, skill_lines, project_subtitles = [], [], []
        raw_bullets = parsed.get("project_updates", parsed.get("project_bullets", parsed.get("bullets", [])))
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
            seen_project_slots.add(slot)
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
            }
            evidence["claims"].append(claim)
            project_bullets.append({"slot": slot, "text": text, **claim})
        for item in parsed.get("skill_lines_ranked", parsed.get("skill_lines", []))[:5]:
            category, values = (item.get("category") or "").strip(), (item.get("items") or "").strip()
            src = item.get("source_asset_id")
            text = f"{category}: {values}"
            if not category or not values or not check(src, text):
                continue
            lines.append(f"- {text}")
            used.append(src)
            claim = {"claim": text, "source_asset_id": src, "kind": "skill_line"}
            evidence["claims"].append(claim)
            skill_lines.append({"category": category, "items": values, "source_asset_id": src})
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
            seen_subtitle_slots.add(slot)
            used.append(src)
            lines.append(f"- [Project subtitle slot {slot}] {text}")
            claim = {
                "claim": text, "source_asset_id": src, "kind": "resume_project_subtitle_change", "slot": slot,
                "previous_subtitle": normalize_subtitle(item.get("previous_subtitle")),
                "jd_requirement_quote": normalize_subtitle(item.get("jd_requirement_quote")),
                "project_evidence_quote": normalize_subtitle(item.get("project_evidence_quote")),
                "word_change_rationale": item.get("word_change_rationale"),
                "why_better": normalize_subtitle(item.get("why_better")),
            }
            evidence["claims"].append(claim)
            project_subtitles.append({"slot": slot, "text": text, **claim})
        evidence["resume_template"] = {
            "project_bullets": project_bullets, "skill_lines": skill_lines,
            "project_subtitles": project_subtitles,
        }

    elif doc_type == "cover_letter":
        company_specific_count = 0
        for p in parsed.get("paragraphs", []):
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
            if src in valid_asset_ids:
                used.append(src)
            if uses_company_context:
                company_specific_count += 1
            evidence["claims"].append({
                "claim": text,
                "source_asset_id": src,
                "kind": "cover_letter_evidence" if src in valid_asset_ids else "cover_letter_structure",
                "purpose": p.get("purpose", ""),
                "jd_requirement_quote": jd_quote,
                "candidate_evidence_quote": evidence_quote,
                "uses_company_context": uses_company_context,
                "company_source_urls": company_urls,
                "company_insight": company_insight,
                "company_evidence_quote": company_evidence_quote,
                "why_company_fit": why_company_fit,
            })
        if valid_company_urls and not company_specific_count:
            dropped.append("Cover letter has company research sources but no verified company-specific paragraph.")
            lines, used, evidence["claims"] = [], [], []
        if lines and company.strip() and job_title.strip():
            opening = f"I am applying for the {job_title.strip()} position at {company.strip()}."
            closing = "Thank you for considering my application."
            lines = [opening, *lines, closing]
            evidence["claims"] = [
                {"claim": opening, "source_asset_id": None,
                 "kind": "cover_letter_structure", "purpose": "opening"},
                *evidence["claims"],
                {"claim": closing, "source_asset_id": None,
                 "kind": "cover_letter_structure", "purpose": "closing"},
            ]

    elif doc_type == "short_answers":
        for a in parsed.get("answers", []):
            q = (a.get("question") or "").strip()
            if not a.get("answerable", False):
                lines.append(f"### {q}\n\n[NEEDS USER INPUT] {a.get('missing_information', '')}")
                evidence["claims"].append({
                    "claim": q, "source_asset_id": None, "answerable": False,
                    "missing_information": a.get("missing_information", ""),
                })
                continue
            text, src = (a.get("text") or "").strip(), a.get("source_asset_id")
            if not text or not check(src, text):
                continue
            lines.append(f"### {q}\n\n{text}")
            used.append(src)
            evidence["claims"].append({
                "claim": text, "source_asset_id": src, "answerable": True,
            })

    evidence["not_supported"] = parsed.get("not_supported", [])
    evidence["model_self_check"] = parsed.get("self_check", "")
    evidence["dropped_ungrounded_claims"] = dropped

    separator = "\n" if doc_type == "resume" else "\n\n"
    return separator.join(lines), sorted(set(used)), evidence, dropped


# ---------------------------------------------------------------- persistence

def next_version(cur, application_id: str, doc_type: str) -> int:
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
    args = p.parse_args()

    if args.doc_type == "short_answers" and not args.question:
        print("ERROR: --doc-type short_answers requires at least one --question.")
        return 2

    print("===== DOCUMENT GENERATOR V1 =====")
    print(f"Generator: {GENERATOR_VERSION}")
    print(f"Doc type:  {args.doc_type}")
    print(f"Mode:      {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Model:     {args.model}\n")

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
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

            assets = fetch_source_assets(cur, app["role_family"])
            if not assets:
                print("ERROR: no approved profile assets. Approve assets before generating.")
                return 1

            valid_ids = {a["profile_asset_id"] for a in assets}
            resume_project_assets = fixed_project_asset_ids(assets)
            resume_subtitle_baselines: dict[int, str] = {}
            resume_bullet_baselines: dict[int, str] = {}
            if args.doc_type == "resume":
                try:
                    resume_subtitle_baselines = load_template_subtitle_baselines()
                    resume_bullet_baselines = load_template_bullet_baselines()
                except (ResumeHeaderAuditError, ResumeBulletAuditError) as exc:
                    print(f"ERROR: resume auditing needs the fixed Word template: {exc}")
                    return 1
            valid_company_urls = set((app.get("company_context") or {}).get("sources") or [])

            field = {
                "resume": "resume_bullet_bank",
                "cover_letter": "cover_letter_positioning",
                "short_answers": "job_oriented_summary",
            }[args.doc_type]
            catalog = render_asset_catalog(assets, field=field)

            if args.doc_type == "resume":
                prompt = build_resume_prompt(
                    app, catalog, args.max_bullets, resume_project_assets, resume_subtitle_baselines,
                    resume_bullet_baselines,
                )
            elif args.doc_type == "cover_letter":
                prompt = build_cover_letter_prompt(app, catalog)
            else:
                prompt = build_short_answer_prompt(app, catalog, args.question)

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
            raw = ollama_generate(
                model=args.model, prompt=prompt, ollama_url=args.ollama_url,
                timeout=args.timeout, temperature=args.temperature, num_ctx=args.ctx,
            )
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

            parsed = extract_json_object(raw)
            content, used, evidence, dropped = validate_and_render(
                args.doc_type, parsed, valid_ids, valid_company_urls,
                resume_project_assets if args.doc_type == "resume" else None,
                args.max_bullets if args.doc_type == "resume" else 12,
                resume_subtitle_baselines if args.doc_type == "resume" else None,
                resume_bullet_baselines if args.doc_type == "resume" else None,
                app["jd_text"],
                app["company"] if args.doc_type == "cover_letter" else "",
                app["job_title"] if args.doc_type == "cover_letter" else "",
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
                conn.rollback()
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
