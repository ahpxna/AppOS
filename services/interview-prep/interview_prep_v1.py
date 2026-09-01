"""
L9 -- INTERVIEW PREP

Creates a lightweight prep package once an interview invite has been
classified. The prep package is intentionally small: the goal is to turn an
interview row into something the candidate can actually use without inventing
facts or building a new knowledge subsystem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly (`python services/interview-prep/interview_prep_v1.py`),
# which is how run_pipeline_chunk.sh and the README invoke every script.
# Without this, `from services.common...` below raises ModuleNotFoundError
# unless the caller happens to have the repo root on PYTHONPATH already.
# Confirmed live 2026-08-01.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.observability import emit_trace, make_trace_id
from services.common.llm_gateway import generate_text
from services.common.model_config import get_model
from services.common.config import database_dsn
from services.common.ai_contracts import parse_json_object as _parse_contract_json_object
from services.common.company_research_sources import (
    company_research_field_evidence, company_research_source_urls,
)

MODEL = get_model("interview_prep")
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
PREP_VERSION = "interview_prep_v1_2026_07_31"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def extract_json_object(raw: str) -> Dict[str, Any]:
    return _parse_contract_json_object(raw, error_message="Interview-prep output JSON must be an object.")



def ollama_generate(*, model: str, prompt: str, ollama_url: str,
                    timeout: int, temperature: float, num_ctx: int) -> str:
    return generate_text(role="interview_prep", model=model, prompt=prompt,
                         local_url=ollama_url, timeout=timeout,
                         temperature=temperature, num_ctx=num_ctx)


def fetch_context_pack(cur) -> str:
    cur.execute(
        """
        SELECT context_text
        FROM profile_context_packs
        WHERE purpose = 'base_interview_prep'
        ORDER BY created_at DESC
        LIMIT 1;
        """
    )
    row = cur.fetchone()
    return row[0] if row else ""


def fetch_research(cur, company: str, application_id: str | None = None) -> Dict[str, Any]:
    from services.common.company_identity_v1 import company_identity_key, employer_domain_from_job_url
    job_url = None
    if application_id:
        cur.execute("SELECT job_url FROM applications WHERE id=%s;", (application_id,))
        app = cur.fetchone()
        job_url = app[0] if app else None
    identity_key = company_identity_key(company, employer_domain_from_job_url(job_url))
    cur.execute(
        """
        SELECT company_domain, summary, mission, products, recent_news, risks, sources
        FROM company_research_cache
        WHERE identity_key = %s
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY last_refreshed_at DESC NULLS LAST
        LIMIT 1;
        """,
        (identity_key,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    sources = sorted(company_research_source_urls(row[6]))
    field_evidence = company_research_field_evidence(row[6])
    evidence_aware = isinstance(row[6], dict) and "field_evidence" in row[6]
    return {
        "company_domain": row[0] or "",
        "summary": (row[1] or "") if evidence_aware and field_evidence.get("summary") else "",
        "mission": (row[2] or "") if evidence_aware and field_evidence.get("mission") else "",
        "products": (row[3] or "") if evidence_aware and field_evidence.get("products") else "",
        "recent_news": [
            item for item in (row[4] or [])
            if isinstance(item, dict) and item.get("source_url") in sources
            and str(item.get("supporting_quote") or "").strip()
        ],
        "risks": [
            item for item in (row[5] or [])
            if isinstance(item, dict) and item.get("source_url") in sources
            and str(item.get("supporting_quote") or "").strip()
        ],
        "sources": sources,
        "field_evidence": field_evidence,
    }


def fetch_queue(cur, interview_id: Optional[str], *, limit: int = 20) -> List[Dict[str, Any]]:
    if interview_id:
        cur.execute(
            """
            SELECT interview_id::text, application_id::text, interview_type,
                   scheduled_at, timezone, company, job_title, fit_score,
                   fit_decision
            FROM v_interviews_pending_prep
            WHERE interview_id = %s;
            """,
            (interview_id,),
        )
    else:
        cur.execute(
            """
            SELECT interview_id::text, application_id::text, interview_type,
                   scheduled_at, timezone, company, job_title, fit_score,
                   fit_decision
            FROM v_interviews_pending_prep
            LIMIT %s;
            """,
            (max(1, min(int(limit), 100)),),
        )
    rows = cur.fetchall()
    return [
        {
            "interview_id": row[0],
            "application_id": row[1],
            "interview_type": row[2],
            "scheduled_at": row[3],
            "timezone": row[4],
            "company": row[5],
            "job_title": row[6],
            "fit_score": row[7],
            "fit_decision": row[8],
        }
        for row in rows
    ]


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def normalize_prep_result(parsed: Any) -> Dict[str, Any]:
    """Normalize nested model fields without turning schema drift into prose.

    Interview prep is advisory.  Missing/malformed text should produce a
    conservative review note, not abort the whole prep batch or stringify a
    model-produced container into user-facing content.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    text = lambda value: value.strip() if isinstance(value, str) else ""
    prep_notes = text(parsed.get("prep_notes")) or (
        "No grounded interview-prep notes were produced from the available information; review the role and approved profile context manually."
    )
    return {
        "prep_notes": prep_notes,
        "opening_line": text(parsed.get("opening_line")),
        "questions_to_ask": _string_list(parsed.get("questions_to_ask")),
        "stories_to_practice": _string_list(parsed.get("stories_to_practice")),
        "watch_outs": _string_list(parsed.get("watch_outs")),
        "self_check": text(parsed.get("self_check")),
        "prep_version": PREP_VERSION,
    }


def build_prompt(interview: Dict[str, Any], context_pack: str, research: Dict[str, Any]) -> str:
    return f"""You are JobOS Interview Prep Agent V1.

Create a concise interview prep package for the candidate.

Rules:
- Do not invent experience.
- Use only the approved profile context pack and company research below.
- Keep the result practical: talking points, questions to ask, and risks.
- If research is missing, say so instead of guessing.

INTERVIEW
Company: {interview['company']}
Role: {interview['job_title']}
Interview type: {interview['interview_type'] or 'unknown'}
Fit score: {interview['fit_score']} ({interview['fit_decision']})
Scheduled at: {interview['scheduled_at'] or 'unscheduled'} {interview['timezone'] or ''}

APPROVED PROFILE CONTEXT PACK
{context_pack or '(none)'}

COMPANY RESEARCH
{json.dumps(research, indent=2, ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "prep_notes": "short practical notes for the user",
  "opening_line": "one short opening line",
  "questions_to_ask": ["3-6 questions the user can ask"],
  "stories_to_practice": ["3-6 bounded STAR/story prompts"],
  "watch_outs": ["risks or weak spots to mention carefully"],
  "self_check": "one sentence confirming no unsupported claims were added"
}}
"""


def cmd_prep(conn, args) -> int:
    with conn.cursor() as cur:
        interviews = fetch_queue(cur, args.interview_id, limit=args.limit)
        if not interviews:
            print("Nothing to prep.")
            return 0

        if args.list_only:
            for interview in interviews:
                print(
                    f"  {interview['interview_id']}  {interview['company']} / "
                    f"{interview['job_title']}  ({interview['interview_type'] or 'unknown'}, "
                    f"scheduled {interview['scheduled_at'] or 'unscheduled'})"
                )
            print(f"\n{len(interviews)} interview(s) pending prep.")
            return 0

        context_pack = fetch_context_pack(cur)
        for interview in interviews:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0));",
                (f"jobos-interview:{interview['interview_id']}:prep",),
            )
            cur.execute(
                "SELECT prep_package_id::text,status FROM interviews WHERE id=%s FOR UPDATE;",
                (interview["interview_id"],),
            )
            locked_interview = cur.fetchone()
            if not locked_interview or locked_interview[0] or str(locked_interview[1] or "") != "prep_needed":
                continue
            os.environ["JOBOS_APPLICATION_ID"] = str(interview["application_id"])
            os.environ["JOBOS_LLM_REQUEST_SCOPE"] = f"interview:{interview['interview_id']}:prep"
            prompt = build_prompt(interview, context_pack, fetch_research(
                cur, interview["company"], interview.get("application_id")
            ))
            trace_id = make_trace_id("interview-prep", interview["interview_id"])
            start = time.perf_counter()
            raw = ollama_generate(
                model=args.model, prompt=prompt, ollama_url=args.ollama_url,
                timeout=args.timeout, temperature=0.2, num_ctx=args.ctx,
            )
            emit_trace(
                trace_id,
                "interview_prep",
                started_at=start,
                tokens_in=estimate_tokens(prompt),
                tokens_out=estimate_tokens(raw),
                cost_usd=0.0,
                interview_id=interview["interview_id"],
                application_id=interview["application_id"],
            )
            parsed = extract_json_object(raw)
            prep_json = normalize_prep_result(parsed)
            prep_notes = prep_json["prep_notes"]

            print(f"\n  {interview['company']} / {interview['job_title']}")
            print(f"    prep notes: {prep_notes[:120]}")

            if args.apply:
                cur.execute(
                    """
                    INSERT INTO interview_prep_packages
                      (interview_id, application_id, model_name, prep_json,
                       prep_notes, qa_status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'pass', now(), now())
                    RETURNING id::text;
                    """,
                    (
                        interview["interview_id"],
                        interview["application_id"],
                        args.model,
                        Jsonb(prep_json),
                        prep_notes,
                    ),
                )
                prep_package_id = cur.fetchone()[0]
                cur.execute(
                    """
                    UPDATE interviews
                    SET prep_package_id = %s,
                        prep_notes = %s,
                        status = 'prepped',
                        updated_at = now()
                    WHERE id = %s;
                    """,
                    (prep_package_id, prep_notes, interview["interview_id"]),
                )
                from services.review.review_service_v1 import ensure_action_required_review
                ensure_action_required_review(
                    cur,
                    application_id=str(interview["application_id"]),
                    action_kind="interview_prep_ready",
                    title=f"Interview prep ready — {interview['company']}",
                    summary=prep_notes,
                    payload={
                        "interview_id": str(interview["interview_id"]),
                        "prep_package_id": str(prep_package_id),
                        "interview_type": interview.get("interview_type") or "NaN",
                        "scheduled_at": str(interview.get("scheduled_at") or "NaN"),
                        "prep": prep_json,
                    },
                    priority="high",
                )

        if not args.apply:
            conn.rollback()
            print("\nDRY RUN. No interview/domain rows committed. LLM transport/accounting may already be durable, and paid providers may incur cost.")
            return 0
        conn.commit()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L9 interview prep")
    p.add_argument("--interview-id")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--list-only", action="store_true",
                    help="Print the pending-prep queue and exit; no LLM calls, no writes.")
    p.add_argument("--limit", type=int, default=20,
                   help="Bounded number of pending interviews to prepare in one run (default: 20).")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--ctx", type=int, default=8192)
    args = p.parse_args()

    print("===== INTERVIEW PREP (L9) =====")
    print(f"Model: {args.model}")

    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        try:
            return cmd_prep(conn, args)
        except RuntimeError as e:
            conn.rollback()
            print(f"\nERROR: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
