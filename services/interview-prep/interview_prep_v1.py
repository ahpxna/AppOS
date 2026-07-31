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
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from services.common.observability import emit_trace, make_trace_id

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

MODEL = os.getenv("JOBOS_INTERVIEW_PREP_MODEL", os.getenv("JOBOS_REPLY_MODEL", "qwen3:8b"))
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
PREP_VERSION = "interview_prep_v1_2026_07_31"


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
        raise ValueError("No JSON object found in prep output.")
    return json.loads(cleaned[first:last + 1])


def ollama_generate(*, model: str, prompt: str, ollama_url: str,
                    timeout: int, temperature: float, num_ctx: int) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        ollama_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")).get("response", "")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama request failed: {e}") from e


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


def fetch_research(cur, company: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT company_domain, summary, mission, products, recent_news, risks
        FROM company_research_cache
        WHERE lower(company_name) = lower(%s)
        ORDER BY last_refreshed_at DESC NULLS LAST
        LIMIT 1;
        """,
        (company,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    return {
        "company_domain": row[0],
        "summary": row[1],
        "mission": row[2],
        "products": row[3],
        "recent_news": row[4] or [],
        "risks": row[5] or [],
    }


def fetch_queue(cur, interview_id: Optional[str]) -> List[Dict[str, Any]]:
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
            FROM v_interviews_pending_prep;
            """
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
        interviews = fetch_queue(cur, args.interview_id)
        if not interviews:
            print("Nothing to prep.")
            return 0

        context_pack = fetch_context_pack(cur)
        for interview in interviews:
            prompt = build_prompt(interview, context_pack, fetch_research(cur, interview["company"]))
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
            prep_notes = (parsed.get("prep_notes") or "").strip()
            if not prep_notes:
                raise RuntimeError("Prep package did not include prep_notes.")
            prep_json = {
                "opening_line": parsed.get("opening_line", ""),
                "questions_to_ask": parsed.get("questions_to_ask", []),
                "stories_to_practice": parsed.get("stories_to_practice", []),
                "watch_outs": parsed.get("watch_outs", []),
                "self_check": parsed.get("self_check", ""),
                "prep_version": PREP_VERSION,
            }

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

        if not args.apply:
            conn.rollback()
            print("\nDRY RUN. Nothing committed.")
            return 0
        conn.commit()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L9 interview prep")
    p.add_argument("--interview-id")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--ctx", type=int, default=8192)
    args = p.parse_args()

    print("===== INTERVIEW PREP (L9) =====")
    print(f"Model: {args.model}")

    with psycopg.connect(DSN, autocommit=False) as conn:
        try:
            return cmd_prep(conn, args)
        except RuntimeError as e:
            conn.rollback()
            print(f"\nERROR: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
