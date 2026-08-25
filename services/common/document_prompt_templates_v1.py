"""Reusable, policy-locked prompt templates for JD-targeted documents.

These templates deliberately separate three kinds of language:
- factual/technical/experience claims: must be grounded in approved user assets;
- resume alignment: maximize JD coverage without changing immutable identity/job data;
- cover-letter self-positioning: may mirror the JD as intent/working style, but may
  not invent historical achievements, technical ability, employment, or metrics.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

RESUME_TARGET_COVERAGE_PERCENT = 80
COVER_POSITIONING_TARGET_PERCENT = 100  # 100% of the small A/I/S subset the letter intentionally selects

GROUNDING_RULES = """
Grounding rules (hard constraints):
1. Precise factual/technical claims -- named tools or systems, credentials,
   metrics, quantified outcomes, production ownership, project facts, education,
   certifications, dates, or other externally verifiable specifics -- must cite
   exactly one approved user ASSET and an exact evidence quote from that asset.
2. Resume experience bullets are the narrow exception: they may be rewritten
   JD-first from the immutable job title + existing bullet without a source asset
   when the rewrite stays general and role-plausible. A new precise factual or
   technical detail still requires approved user evidence.
3. Do not merge evidence from multiple assets into one factual claim.
4. Never change employers, job titles, dates, degrees, certifications, or
   clearances. Do not invent metrics, tools, credentials, or outcomes.
5. Honour every MUST NOT CLAIM line.
6. Academic/course/research work stays academic/course/research work. Never
   convert it into professional or production experience.
7. Missing information is not a generation failure. Omit unsupported specifics,
   lower specificity, preserve safe baseline content, and record a warning.
""".strip()


def requirement_catalog(matched_requirements: Sequence[Any]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for index, item in enumerate(matched_requirements or [], start=1):
        if isinstance(item, Mapping):
            requirement = str(item.get("requirement") or "").strip()
            support = str(item.get("profile_support") or "").strip()
            boundary = str(item.get("evidence_boundary") or "").strip()
        else:
            requirement, support, boundary = str(item or "").strip(), "", ""
        if requirement:
            catalog.append({
                "id": f"M{index}", "requirement": requirement,
                "profile_support": support, "evidence_boundary": boundary,
            })
    return catalog


def material_requirement_summary(app: Mapping[str, Any]) -> dict[str, Any]:
    """Return the truthful full-JD coverage denominator used by resume tailoring.

    ``matched_requirements`` are the requirements the current approved profile can
    support. ``missing_or_weak_requirements`` are material JD requirements that
    cannot safely be claimed yet. The target is 80% of the combined denominator;
    when evidence cannot reach 80%, the truthful ceiling is explicit and the agent
    must cover every supportable requirement rather than fabricate the gap.
    """
    supportable = requirement_catalog(app.get("matched_requirements") or [])
    seen = {item["requirement"].casefold() for item in supportable}
    unsupported = []
    for item in app.get("missing_or_weak_requirements") or []:
        if isinstance(item, Mapping):
            requirement = str(item.get("requirement") or item.get("name") or "").strip()
            severity = str(item.get("severity") or "").strip()
        else:
            requirement, severity = str(item or "").strip(), ""
        if not requirement or requirement.casefold() in seen:
            continue
        seen.add(requirement.casefold())
        unsupported.append({"id": f"U{len(unsupported) + 1}", "requirement": requirement, "severity": severity})

    total = len(supportable) + len(unsupported)
    ceiling = (100.0 * len(supportable) / total) if total else 100.0
    required_supportable = (
        min(len(supportable), math.ceil(RESUME_TARGET_COVERAGE_PERCENT * total / 100.0))
        if total else 0
    )
    return {
        "supportable": supportable,
        "unsupported": unsupported,
        "total_material_requirements": total,
        "truthful_coverage_ceiling_percent": round(ceiling, 1),
        "target_reachable_truthfully": ceiling >= RESUME_TARGET_COVERAGE_PERCENT,
        "required_supportable_covered_count": required_supportable,
    }


def build_resume_tailoring_prompt(
    *, app: Mapping[str, Any], asset_catalog: str, max_project_bullets: int,
    fixed_projects: Sequence[str], fixed_project_asset_rules: str,
    baseline_subtitles: Mapping[int, str], baseline_project_bullets: Mapping[int, str],
    experience_baselines: Mapping[int, Mapping[str, Any]],
) -> str:
    coverage = material_requirement_summary(app)
    matched = coverage["supportable"]
    return f"""You are JobOS Resume Tailoring Agent V3.

GOAL
Tailor the existing resume aggressively toward the specific JD. AIM for at least
{RESUME_TARGET_COVERAGE_PERCENT}% of ALL MATERIAL JD REQUIREMENTS. This is a
quality target, NOT a hard-fail gate. If available information cannot support that
coverage, keep the strongest safe edits, preserve unchanged baseline content,
record the achieved coverage/warnings, and continue. NEVER invent precise
technical facts, metrics, credentials, tools, or outcomes just to hit the number.

IMMUTABLE DATA
- Keep every employer/company name, job title, date, location, education entry,
  certification, project name, project date, GitHub URL, contact field, and
  identity field unchanged.
- Experience bullet descriptions MAY be rewritten JD-first to match the target
  requirement and the immutable existing job title. They do NOT need a verbatim
  official-resume evidence quote when kept general and role-plausible.
- Any NEW precise tool/system/metric/credential/quantified outcome or other
  externally verifiable technical fact still needs approved user evidence.
- Project bullets/subtitles and skills MAY be tailored under their fixed-slot
  contracts.

TARGET ROLE: {app.get('job_title') or ''} at {app.get('company') or ''}
ROLE FAMILY: {app.get('role_family') or ''}
SENIORITY: {app.get('seniority_level') or ''}

FULL JOB DESCRIPTION (authoritative for wording/alignment):
{app.get('jd_text') or ''}

SUPPORTABLE MATERIAL REQUIREMENTS (claimable with approved user evidence):
{json.dumps(matched, indent=2, ensure_ascii=False)}

UNSUPPORTED MATERIAL REQUIREMENTS (part of full-JD denominator; never fabricate):
{json.dumps(coverage['unsupported'], indent=2, ensure_ascii=False)}

COVERAGE ACCOUNTING:
- total_material_requirements = {coverage['total_material_requirements']}
- truthful_coverage_ceiling_percent = {coverage['truthful_coverage_ceiling_percent']}
- required_supportable_covered_count = {coverage['required_supportable_covered_count']}
- target_reachable_truthfully = {str(coverage['target_reachable_truthfully']).lower()}

{GROUNDING_RULES}

APPROVED USER ASSETS:
{asset_catalog}

EXPERIENCE BULLET BASELINES
Only bullet text may change. `header_context` is immutable binding context; copy
it exactly. Prioritize the FULL JD and the meaning/scope of that existing job
title over the sparse wording of the old bullet. The old bullet is a scope anchor,
not a phrase lock. A general JD-aligned rewrite may use `source_asset_id: "none"`.
If you introduce a named tool/system, metric, credential, quantified result,
production-ownership claim, or similarly precise technical fact, cite one approved
asset and an exact evidence quote. Keep the rewrite at or below max_chars.
{json.dumps(dict(experience_baselines or {}), indent=2, ensure_ascii=False)}

FIXED PROJECTS (title/date/link immutable): {', '.join(fixed_projects)}
CURRENT PROJECT SUBTITLE BASELINE:
{json.dumps(dict(baseline_subtitles or {}), indent=2, ensure_ascii=False)}
CURRENT PROJECT BULLET BASELINE:
{json.dumps(dict(baseline_project_bullets or {}), indent=2, ensure_ascii=False)}
ALLOWED PROJECT ASSET IDS BY SLOT:
{fixed_project_asset_rules}

COVERAGE RULE
- Project/skill/subtitle items must list one or more `matched_requirement_ids`
  from the supportable catalog (M1, M2, ...).
- Experience rewrites should list M IDs when genuinely supportable, but MAY leave
  the list empty when the JD/job-title rewrite is useful yet current profile data
  cannot substantiate that requirement strongly enough for coverage credit.
- Use an ID only when the item materially addresses that requirement.
- Coverage is measured against ALL material requirements above, not only M IDs.
- Aim to cover at least {coverage['required_supportable_covered_count']} distinct supportable M IDs.
  If that is not possible with safe output, keep the best lower-coverage result,
  preserve baseline content, and record a warning instead of failing generation.
- Do not keyword-stuff several IDs onto unrelated content.

Return ONLY valid JSON, no markdown:
{{
  "experience_updates": [
    {{
      "slot": 1,
      "header_context": "exact immutable header_context from baseline",
      "text": "rewritten existing experience bullet",
      "source_asset_id": "none for general JD/job-title reframing; approved ASSET uuid only when needed for a precise factual/technical addition",
      "previous_bullet": "exact baseline bullet",
      "jd_requirement_quote": "exact verbatim phrase from FULL JOB DESCRIPTION",
      "experience_evidence_quote": "empty for general reframing; exact quote from cited ASSET when a precise factual/technical addition needs evidence",
      "matched_requirement_ids": ["M1"],
      "word_change_rationale": [
        {{"before":"old phrase","after":"new phrase","why":"specific JD + user-evidence reason"}}
      ],
      "why_better": "specific reason this is more relevant without changing the factual scope"
    }}
  ],
  "project_updates": [
    {{
      "slot": 1,
      "text": "one project bullet",
      "source_asset_id": "<approved ASSET uuid>",
      "previous_bullet": "exact current project bullet baseline",
      "jd_requirement_quote": "exact JD phrase",
      "project_evidence_quote": "exact phrase from matching ASSET",
      "matched_requirement_ids": ["M1"],
      "word_change_rationale": [
        {{"before":"old phrase","after":"new phrase","why":"specific JD + project-evidence reason"}}
      ],
      "why_better": "specific relevance gain",
      "evidence_boundary": "academic project | coursework | research | lab exercise"
    }}
  ],
  "skill_lines_ranked": [
    {{
      "category": "existing resume skill category",
      "items": "comma-separated grounded skills",
      "source_asset_id": "<approved ASSET uuid>",
      "matched_requirement_ids": ["M1"],
      "jd_requirement_quote": "exact JD phrase",
      "skill_evidence_quote": "exact phrase from cited ASSET"
    }}
  ],
  "project_subtitle_updates": [
    {{
      "slot": 1,
      "text": "replacement subtitle only",
      "source_asset_id": "<matching project ASSET uuid>",
      "previous_subtitle": "exact template subtitle",
      "jd_requirement_quote": "exact JD phrase",
      "project_evidence_quote": "exact ASSET phrase",
      "matched_requirement_ids": ["M1"],
      "word_change_rationale": [
        {{"before":"old phrase","after":"new phrase","why":"specific JD + evidence reason"}}
      ],
      "why_better": "specific relevance gain"
    }}
  ],
  "not_supported": ["JD requirements deliberately left out because user evidence cannot support them"],
  "self_check": "confirm immutable job titles/employers/dates were not changed and no factual claim was invented"
}}

Project-specific constraints:
- Select only genuinely relevant project slots backed by the allowed asset IDs.
- Odd project bullet slots: <=200 chars; even slots: <=105 chars. Never emit an
  even slot without its preceding odd slot.
- Project subtitle: <=88 chars and must remain inside its immutable project header.
- Produce at most {min(max_project_bullets, 12)} project bullets.
- Skills must be explicit in approved user evidence; JD keywords alone are not evidence.
- Fewer truthful items are better than fabricated coverage.
"""


def build_cover_alignment_blueprint_prompt(*, app: Mapping[str, Any]) -> str:
    return f"""You are JobOS JD Alignment Extractor V1.

Read the FULL JD and extract a useful candidate pool for a cover letter. Use ONLY exact verbatim quotes from the JD. Do not infer hidden
requirements and do not paraphrase the quote fields.

TARGET ROLE: {app.get('job_title') or ''} at {app.get('company') or ''}
FULL JOB DESCRIPTION:
{app.get('jd_text') or ''}

Classify explicit JD language into:
- about_me_targets: role identity / type of person / approach the JD is seeking.
- interest_targets: problem domain, mission, customer/product/problem themes a
  candidate can truthfully express interest in because the JD itself states them.
- soft_skill_targets: collaboration, communication, ownership style, curiosity,
  adaptability, organization, stakeholder behavior, learning style, etc.
- technical_targets: tools, technologies, methods, systems, technical duties,
  credentials, years, or factual experience requirements.

A quote may appear in more than one category only if the JD literally combines
those concepts. Prioritize the clearest, most distinctive targets; this is a
candidate pool, not an obligation to mention every soft skill in the JD.

Return ONLY valid JSON:
{{
  "about_me_targets": [{{"quote":"exact JD quote","why":"short classification reason"}}],
  "interest_targets": [{{"quote":"exact JD quote","why":"short classification reason"}}],
  "soft_skill_targets": [{{"quote":"exact JD quote","why":"short classification reason"}}],
  "technical_targets": [{{"quote":"exact JD quote","why":"short classification reason"}}]
}}
"""


def build_cover_alignment_audit_prompt(
    *, app: Mapping[str, Any], alignment_blueprint: Mapping[str, Any],
) -> str:
    return f"""You are JobOS Cover-Letter Alignment Candidate Auditor V2.

The first extractor produced the candidate pool below. Re-read the FULL JD and
add only high-value about-me, role-interest, soft-skill, or technical candidates
that would materially improve tailoring. This is NOT an exhaustive coverage gate.

FULL JOB DESCRIPTION:
{app.get('jd_text') or ''}

CURRENT BLUEPRINT:
{json.dumps(dict(alignment_blueprint or {}), indent=2, ensure_ascii=False)}

Rules:
- Return ONLY targets missing from the current blueprint.
- Every quote MUST be an exact verbatim substring of the full JD.
- Do not chase exhaustive A/I/S coverage; return only distinctive useful additions.
- Also report high-value technical targets so the final letter knows what may be
  evidence-bound or omitted.
- Do not infer personality or requirements not explicitly stated in the JD.

Return ONLY valid JSON:
{{
  "about_me_targets": [{{"quote":"exact omitted JD quote","why":"why this is an about-me target"}}],
  "interest_targets": [{{"quote":"exact omitted JD quote","why":"why this is an interest target"}}],
  "soft_skill_targets": [{{"quote":"exact omitted JD quote","why":"why this is a soft-skill target"}}],
  "technical_targets": [{{"quote":"exact omitted JD quote","why":"why this is technical/factual"}}]
}}
"""



def build_cover_letter_tailoring_prompt(
    *, app: Mapping[str, Any], asset_catalog: str, alignment_blueprint: Mapping[str, Any],
) -> str:
    return f"""You are JobOS Cover Letter Tailoring Agent V2.

GOAL
Write a concise cover letter that is maximally tailored to this JD while
separating subjective positioning from factual evidence.

TARGET ROLE: {app.get('job_title') or ''} at {app.get('company') or ''}
FULL JOB DESCRIPTION:
{app.get('jd_text') or ''}

ALIGNMENT BLUEPRINT (validated exact JD quotes):
{json.dumps(dict(alignment_blueprint or {}), indent=2, ensure_ascii=False)}

SOURCED COMPANY CONTEXT (optional; company-specific facts require exact source URLs):
{json.dumps(app.get('company_context') or {}, indent=2, ensure_ascii=False)}

APPROVED USER ASSETS (only source of factual/technical candidate claims):
{asset_catalog}

{GROUNDING_RULES}

COVER-LETTER POLICY
1. SELECTED FIT, NOT CHECKLIST COVERAGE: choose only a small subset of the A/I/S
   candidates that best fit the user's background and create a positive employer
   impression. Usually 2-5 total A/I/S targets across the whole letter is enough;
   you do NOT need to mention every A/I/S item in the JD.
2. ABOUT ME / INTEREST / SOFT SKILLS are subjective positioning. They may be
   written directly from the JD even when no user source-of-truth exists. Phrase
   them as orientation, preference, motivation, or working style -- not invented
   historical achievements.
3. Prefer A/I/S targets that naturally complement the approved user background;
   do not force awkward JD keywords just for coverage.
4. TECHNICAL / EXPERIENCE: every named tool/system, metric, credential, concrete
   project/employment fact, quantified result, or technical capability claim MUST
   cite one approved user asset plus an exact candidate evidence quote. If data is
   missing, omit or generalize the technical claim; do not fail the letter.
5. Each sentence has exactly one claim class. Do not mix subjective positioning
   and a factual technical claim in one sentence.
6. Use 2-4 concise substantive paragraphs when possible. If information is sparse,
   a shorter safe letter is valid. The renderer adds the deterministic application
   sentence and closing.

Return ONLY valid JSON:
{{
  "paragraphs": [
    {{
      "sentences": [
        {{
          "kind": "about_me_positioning | role_interest | soft_skill_positioning | technical_evidence | company_interest",
          "text": "one complete sentence",
          "alignment_ids": ["A1"],
          "jd_requirement_quote": "exact JD quote targeted by this sentence",
          "source_asset_id": "none for positioning/interest; approved uuid for technical_evidence",
          "candidate_evidence_quote": "exact approved ASSET quote for technical_evidence; empty otherwise",
          "uses_company_context": false,
          "company_source_urls": [],
          "company_insight": "source-backed company fact only for company_interest",
          "company_evidence_quote": "exact source-context phrase only for company_interest"
        }}
      ]
    }}
  ],
  "not_supported": ["technical/factual JD claims omitted because approved user data does not support them"],
  "self_check": "confirm the selected A/I/S targets are natural and persuasive, and every technical/factual sentence is user-data grounded; missing information was omitted/generalized rather than causing failure"
}}

Alignment IDs are assigned by category order: about_me A1..An, interest I1..In,
soft skills S1..Sn, technical T1..Tn. Positioning sentences may cite a small
selected subset of A/I/S IDs; they do not need to cover all available IDs. `technical_evidence` may cite T IDs and must be grounded in approved data.
`company_interest` may additionally use sourced company context, but a source URL
never proves a candidate technical skill.
"""


def build_cover_letter_completeness_verifier_prompt(
    *, jd_text: str, cover_text: str, alignment_blueprint: Mapping[str, Any],
) -> str:
    return f"""You are JobOS Cover-Letter Selected-Fit Auditor V3.

Read the FULL JOB DESCRIPTION and FINISHED COVER LETTER. This is an advisory
quality check, NOT an exhaustive A/I/S coverage gate. The letter should mention
only a few A/I/S themes that fit naturally; technical/factual truth is checked
separately against approved user evidence.

FULL JOB DESCRIPTION:
{jd_text}

CANDIDATE ALIGNMENT BLUEPRINT:
{json.dumps(dict(alignment_blueprint or {}), indent=2, ensure_ascii=False)}

FINISHED COVER LETTER:
{cover_text}

Rules:
1. Do NOT require every A/I/S candidate from the JD or blueprint to appear.
2. Evaluate only A/I/S themes the finished letter actually chose to express.
3. The selected themes should be relevant to the JD, coherent with the role, and
   phrased as interest/orientation/working style rather than invented history.
4. Missing information is acceptable. A shorter safe letter is better than an
   invented fact. Unsupported technical targets may be omitted.
5. Return `supported` when the selected positioning is relevant and natural.
   Return `needs_polish` only for awkward/irrelevant positioning; do not report
   unselected JD themes as missing.

Return ONLY valid JSON:
{{
  "verdict": "supported | needs_polish",
  "reason": "short quality note",
  "missing_about_me_quotes": [],
  "missing_interest_quotes": [],
  "missing_soft_skill_quotes": [],
  "unmodeled_about_me_quotes": [],
  "unmodeled_interest_quotes": [],
  "unmodeled_soft_skill_quotes": []
}}
"""

def build_positioning_verifier_prompt(*, claim: str, kind: str, jd_quote: str) -> str:
    return f"""You are JobOS Cover-Letter Positioning Auditor V1.

Decide whether this sentence is a safe subjective positioning statement aligned
to the exact JD quote. It is allowed to express interest, values, preferred
working style, learning orientation, or role fit. It is NOT allowed to assert an
uncited historical achievement, employer fact, technical skill, tool use,
credential, years of experience, metric, project result, leadership event, or
other externally verifiable candidate fact.

KIND: {kind}
SENTENCE: {claim!r}
EXACT JD QUOTE: {jd_quote!r}

Return ONLY valid JSON:
{{"verdict":"supported | rule_violation", "reason":"one concise reason"}}
"""
