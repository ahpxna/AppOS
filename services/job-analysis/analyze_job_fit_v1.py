import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly. Without this, the import below raises
# ModuleNotFoundError unless the caller happens to have the repo root on
# PYTHONPATH already. Confirmed live 2026-08-01 (this file was already
# broken this way before today's fix).
sys.path.insert(0, str(PROJECT_ROOT))
from services.common.observability import emit_trace, make_trace_id
from services.common.model_config import get_model

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

ANALYZER_VERSION = "job_fit_analyzer_v1_profile_pack_2026_04_28"
COMPONENT_NAME = "fit_checker_jd_analyzer"
TASK_TYPE = "jd_fit_check"

DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def read_jd_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"JD file not found: {path}")
    return clean_text(path.read_text(encoding="utf-8", errors="replace"))


def extract_json_object(raw: str) -> Dict[str, Any]:
    cleaned = raw.strip()

    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = cleaned.replace("```json", "```").replace("```JSON", "```")

    fence = re.search(r"```(.*?)```", cleaned, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ValueError("Could not find JSON object in model output.")

    candidate = cleaned[first:last + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse JSON object from model output: {e}") from e


def ollama_generate(
    *,
    model: str,
    prompt: str,
    ollama_url: str,
    timeout: int,
    temperature: float,
    num_ctx: int,
) -> str:
    url = ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama request failed: {e}") from e

    parsed = json.loads(body)
    return parsed.get("response", "")


def fetch_profile_pack(cur) -> Tuple[str, str, str]:
    cur.execute(
        """
        SELECT
          id::text,
          approved_facts_snapshot_hash,
          context_text
        FROM profile_context_packs
        WHERE application_id IS NULL
          AND message_thread_id IS NULL
          AND purpose = 'base_fit_check_support'
        ORDER BY created_at DESC
        LIMIT 1;
        """
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Missing base_fit_check_support profile_context_pack.")
    return str(row[0]), str(row[1]), str(row[2])


def get_or_create_application(
    cur,
    *,
    application_id: Optional[str],
    jd_text: Optional[str],
    company: Optional[str],
    job_title: Optional[str],
    job_url: Optional[str],
    source: str,
) -> Tuple[str, Dict[str, Any]]:
    if application_id:
        cur.execute(
            """
            SELECT
              id::text,
              source,
              company,
              job_title,
              job_url,
              jd_text,
              jd_hash,
              status,
              current_step
            FROM applications
            WHERE id = %s;
            """,
            (application_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Application not found: {application_id}")

        app = {
            "id": row[0],
            "source": row[1],
            "company": row[2],
            "job_title": row[3],
            "job_url": row[4],
            "jd_text": row[5],
            "jd_hash": row[6],
            "status": row[7],
            "current_step": row[8],
        }
        if not app["jd_text"]:
            raise RuntimeError("Application exists but jd_text is empty.")
        return app["id"], app

    if not jd_text:
        raise RuntimeError("Either --application-id or --jd-file is required.")

    jd_hash = sha256_text(jd_text)

    cur.execute(
        """
        SELECT
          id::text,
          source,
          company,
          job_title,
          job_url,
          jd_text,
          jd_hash,
          status,
          current_step
        FROM applications
        WHERE jd_hash = %s
          AND COALESCE(job_url, '') = COALESCE(%s, '')
        ORDER BY created_at DESC
        LIMIT 1;
        """,
        (jd_hash, job_url),
    )
    existing = cur.fetchone()

    if existing:
        app_id = str(existing[0])
        cur.execute(
            """
            UPDATE applications
            SET
              source = COALESCE(%s, source),
              company = COALESCE(%s, company),
              job_title = COALESCE(%s, job_title),
              job_url = COALESCE(%s, job_url),
              jd_text = %s,
              jd_hash = %s,
              updated_at = now()
            WHERE id = %s
            RETURNING
              id::text,
              source,
              company,
              job_title,
              job_url,
              jd_text,
              jd_hash,
              status,
              current_step;
            """,
            (source, company, job_title, job_url, jd_text, jd_hash, app_id),
        )
        row = cur.fetchone()
    else:
        cur.execute(
            """
            INSERT INTO applications (
              source,
              company,
              job_title,
              job_url,
              jd_text,
              jd_hash,
              current_step,
              status,
              created_at,
              updated_at
            )
            VALUES (
              %s, %s, %s, %s, %s, %s,
              'jd_ingested',
              'new',
              now(),
              now()
            )
            RETURNING
              id::text,
              source,
              company,
              job_title,
              job_url,
              jd_text,
              jd_hash,
              status,
              current_step;
            """,
            (source, company, job_title, job_url, jd_text, jd_hash),
        )
        row = cur.fetchone()

    app = {
        "id": row[0],
        "source": row[1],
        "company": row[2],
        "job_title": row[3],
        "job_url": row[4],
        "jd_text": row[5],
        "jd_hash": row[6],
        "status": row[7],
        "current_step": row[8],
    }
    return app["id"], app


def build_prompt(app: Dict[str, Any], profile_pack_text: str) -> str:
    jd_text = clean_text(app["jd_text"] or "")

    return f"""
You are JobOS JD Analyzer / Fit Checker V1.

Your task:
Analyze whether this job is a good fit for the applicant using ONLY the approved profile context pack below and the JD text.
Do not invent experience. Do not assume certifications, citizenship, clearance, work authorization, professional SOC experience, production cloud/SIEM ownership, or employment history unless explicitly present in the approved profile context.

Decision policy:
- fit_score must be 0-100.
- fit_decision must be exactly one of: reject, ask_user, approve_research.
- reject: serious mismatch, seniority too high, hard blocker, or fit_score below 60.
- ask_user: fit_score 60-74, uncertain requirement, or potentially useful role requiring user decision.
- approve_research: fit_score 75+ and no hard blocker; next step is company research.
- Academic/project evidence is allowed for entry-level roles, but must be labeled as academic/project-based.

Return ONLY valid JSON. No markdown. No commentary.

Required JSON schema:
{{
  "fit_score": 0,
  "fit_decision": "reject",
  "decision_reason": "",
  "role_family": "cybersecurity_general | soc_dfir | network_security | appsec_entry_level | grc_security_analytics | software_engineering | other",
  "seniority_level": "",
  "work_mode": "",
  "location": "",
  "salary_range": "",
  "matched_requirements": [
    {{
      "requirement": "",
      "profile_support": "",
      "evidence_boundary": ""
    }}
  ],
  "missing_or_weak_requirements": [
    {{
      "requirement": "",
      "severity": "low | medium | high",
      "quick_learnable": true,
      "note": ""
    }}
  ],
  "quick_learn_targets": [],
  "hard_blockers": [
    {{
      "blocker": "",
      "reason": ""
    }}
  ],
  "risk_flags": [
    {{
      "risk": "",
      "why": ""
    }}
  ],
  "recommended_profile_brief_types": [],
  "next_step": ""
}}

APPLICATION METADATA:
Company: {app.get("company") or ""}
Job title: {app.get("job_title") or ""}
Job URL: {app.get("job_url") or ""}
Source: {app.get("source") or ""}

APPROVED PROFILE CONTEXT PACK:
{profile_pack_text}

JOB DESCRIPTION:
{jd_text}
""".strip()


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def normalize_analysis(parsed: Dict[str, Any]) -> Dict[str, Any]:
    score_raw = parsed.get("fit_score", 0)
    try:
        score = int(score_raw)
    except Exception:
        score = 0
    score = max(0, min(100, score))

    model_decision = str(parsed.get("fit_decision", "ask_user")).strip()
    if model_decision not in {"reject", "ask_user", "approve_research"}:
        model_decision = "ask_user"

    hard_blockers = as_list(parsed.get("hard_blockers"))

    if hard_blockers:
        decision = "reject"
    elif score < 60:
        decision = "reject"
    elif score < 75:
        decision = "ask_user"
    else:
        decision = "approve_research"

    if decision == "approve_research":
        priority = "high" if score >= 85 else "medium_high"
        next_step = parsed.get("next_step") or "approve_research"
    elif decision == "ask_user":
        priority = "review"
        next_step = parsed.get("next_step") or "ask_user_to_review_fit"
    else:
        priority = "low"
        next_step = parsed.get("next_step") or "save_only_reject_by_fit"

    return {
        "fit_score": score,
        "fit_decision": decision,
        "model_fit_decision": model_decision,
        "priority": priority,
        "decision_reason": str(parsed.get("decision_reason") or ""),
        "role_family": str(parsed.get("role_family") or "other"),
        "seniority_level": str(parsed.get("seniority_level") or ""),
        "work_mode": str(parsed.get("work_mode") or ""),
        "location": str(parsed.get("location") or ""),
        "salary_range": str(parsed.get("salary_range") or ""),
        "matched_requirements": as_list(parsed.get("matched_requirements")),
        "missing_or_weak_requirements": as_list(parsed.get("missing_or_weak_requirements")),
        "quick_learn_targets": as_list(parsed.get("quick_learn_targets")),
        "hard_blockers": hard_blockers,
        "risk_flags": as_list(parsed.get("risk_flags")),
        "recommended_profile_brief_types": as_list(parsed.get("recommended_profile_brief_types")),
        "next_step": next_step,
        "extracted_job_fields": {
            "role_family": str(parsed.get("role_family") or "other"),
            "seniority_level": str(parsed.get("seniority_level") or ""),
            "work_mode": str(parsed.get("work_mode") or ""),
            "location": str(parsed.get("location") or ""),
            "salary_range": str(parsed.get("salary_range") or ""),
        },
    }


def app_status_for_decision(decision: str) -> Tuple[str, str]:
    if decision == "approve_research":
        return "fit_approved", "ready_for_company_research"
    if decision == "ask_user":
        return "needs_user_fit_review", "fit_review"
    return "rejected_by_fit", "fit_checked"


def insert_component_run(
    cur,
    *,
    application_id: str,
    model: str,
    input_json: Dict[str, Any],
    output_json: Dict[str, Any],
    raw_output: str,
    prompt: str,
) -> str:
    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,
          task_type,
          application_id,
          input_json,
          output_json,
          output_text,
          status,
          model_provider,
          model_name,
          input_tokens,
          output_tokens,
          estimated_cost_usd,
          finished_at
        )
        VALUES (
          %s, %s, %s,
          %s, %s, %s,
          'completed',
          'ollama_local',
          %s,
          %s,
          %s,
          0,
          now()
        )
        RETURNING id::text;
        """,
        (
            COMPONENT_NAME,
            TASK_TYPE,
            application_id,
            Jsonb(input_json),
            Jsonb(output_json),
            raw_output,
            model,
            estimate_tokens(prompt),
            estimate_tokens(raw_output),
        ),
    )
    return str(cur.fetchone()[0])


def upsert_fit_analysis(
    cur,
    *,
    application_id: str,
    component_run_id: Optional[str],
    profile_context_pack_id: str,
    model: str,
    analysis: Dict[str, Any],
    raw_output: str,
):
    cur.execute(
        """
        INSERT INTO job_fit_analyses (
          application_id,
          component_run_id,
          analyzer_version,
          analyzer_model,
          profile_context_pack_id,

          fit_score,
          fit_decision,
          model_fit_decision,
          priority,

          decision_reason,
          role_family,
          seniority_level,
          work_mode,
          location,
          salary_range,

          matched_requirements,
          missing_or_weak_requirements,
          quick_learn_targets,
          hard_blockers,
          risk_flags,
          recommended_profile_brief_types,
          extracted_job_fields,

          next_step,
          raw_model_output,
          created_at
        )
        VALUES (
          %s, %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s, %s,
          %s, %s, now()
        )
        ON CONFLICT (application_id, analyzer_version)
        DO UPDATE SET
          component_run_id = EXCLUDED.component_run_id,
          analyzer_model = EXCLUDED.analyzer_model,
          profile_context_pack_id = EXCLUDED.profile_context_pack_id,

          fit_score = EXCLUDED.fit_score,
          fit_decision = EXCLUDED.fit_decision,
          model_fit_decision = EXCLUDED.model_fit_decision,
          priority = EXCLUDED.priority,

          decision_reason = EXCLUDED.decision_reason,
          role_family = EXCLUDED.role_family,
          seniority_level = EXCLUDED.seniority_level,
          work_mode = EXCLUDED.work_mode,
          location = EXCLUDED.location,
          salary_range = EXCLUDED.salary_range,

          matched_requirements = EXCLUDED.matched_requirements,
          missing_or_weak_requirements = EXCLUDED.missing_or_weak_requirements,
          quick_learn_targets = EXCLUDED.quick_learn_targets,
          hard_blockers = EXCLUDED.hard_blockers,
          risk_flags = EXCLUDED.risk_flags,
          recommended_profile_brief_types = EXCLUDED.recommended_profile_brief_types,
          extracted_job_fields = EXCLUDED.extracted_job_fields,

          next_step = EXCLUDED.next_step,
          raw_model_output = EXCLUDED.raw_model_output,
          created_at = now()
        RETURNING id::text;
        """,
        (
            application_id,
            component_run_id,
            ANALYZER_VERSION,
            model,
            profile_context_pack_id,

            analysis["fit_score"],
            analysis["fit_decision"],
            analysis["model_fit_decision"],
            analysis["priority"],

            analysis["decision_reason"],
            analysis["role_family"],
            analysis["seniority_level"],
            analysis["work_mode"],
            analysis["location"],
            analysis["salary_range"],

            Jsonb(analysis["matched_requirements"]),
            Jsonb(analysis["missing_or_weak_requirements"]),
            Jsonb(analysis["quick_learn_targets"]),
            Jsonb(analysis["hard_blockers"]),
            Jsonb(analysis["risk_flags"]),
            Jsonb(analysis["recommended_profile_brief_types"]),
            Jsonb(analysis["extracted_job_fields"]),

            analysis["next_step"],
            raw_output,
        ),
    )
    return str(cur.fetchone()[0])


def update_application(cur, *, application_id: str, analysis: Dict[str, Any]):
    status, _legacy_step = app_status_for_decision(analysis["fit_decision"])

    cur.execute(
        """
        UPDATE applications
        SET
          fit_score = %s,
          fit_decision = %s,
          priority = %s,
          status = %s,
          seniority_level = COALESCE(NULLIF(%s, ''), seniority_level),
          work_mode = COALESCE(NULLIF(%s, ''), work_mode),
          location = COALESCE(NULLIF(%s, ''), location),
          salary_range = COALESCE(NULLIF(%s, ''), salary_range),
          updated_at = now()
        WHERE id = %s;
        """,
        (
            analysis["fit_score"],
            analysis["fit_decision"],
            analysis["priority"],
            status,
            analysis["seniority_level"],
            analysis["work_mode"],
            analysis["location"],
            analysis["salary_range"],
            application_id,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-id")
    parser.add_argument("--jd-file")
    parser.add_argument("--company")
    parser.add_argument("--job-title")
    parser.add_argument("--job-url")
    parser.add_argument("--source", default="manual_jd_file")

    parser.add_argument("--model", default=get_model("job_fit"))
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--ctx", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.1)

    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--print-prompt", action="store_true")

    args = parser.parse_args()

    jd_text = None
    if args.jd_file:
        jd_text = read_jd_file(Path(args.jd_file))

    print("===== JOB FIT ANALYZER V1 =====")
    print(f"Analyzer version: {ANALYZER_VERSION}")
    print(f"Mode:             {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Model:            {args.model}")
    print(f"Ollama URL:       {args.ollama_url}")
    print("")

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            profile_pack_id, snapshot_hash, profile_pack_text = fetch_profile_pack(cur)

            application_id, app = get_or_create_application(
                cur,
                application_id=args.application_id,
                jd_text=jd_text,
                company=args.company,
                job_title=args.job_title,
                job_url=args.job_url,
                source=args.source,
            )

            prompt = build_prompt(app, profile_pack_text)

            print(f"Application:      {application_id}")
            print(f"Company:          {app.get('company')}")
            print(f"Job title:        {app.get('job_title')}")
            print(f"JD hash:          {app.get('jd_hash')}")
            print(f"Profile pack:     {profile_pack_id}")
            print(f"Snapshot:         {snapshot_hash}")
            print(f"Prompt tokens~:   {estimate_tokens(prompt)}")
            print("")

            if args.print_prompt:
                print("===== PROMPT =====")
                print(prompt)
                print("===== END PROMPT =====")
                print("")

            start = time.perf_counter()
            raw_output = ollama_generate(
                model=args.model,
                prompt=prompt,
                ollama_url=args.ollama_url,
                timeout=args.timeout,
                temperature=args.temperature,
                num_ctx=args.ctx,
            )
            elapsed = time.perf_counter() - start
            emit_trace(
                make_trace_id("fit", app["id"]),
                "fit_analysis",
                started_at=start,
                tokens_in=estimate_tokens(prompt),
                tokens_out=estimate_tokens(raw_output),
                cost_usd=0.0,
                application_id=app["id"],
                fit_decision="pending_parse",
            )

            parsed = extract_json_object(raw_output)
            analysis = normalize_analysis(parsed)

            print("===== MODEL RESULT =====")
            print(json.dumps(analysis, indent=2, ensure_ascii=False))
            print("")
            print(f"Elapsed seconds:  {elapsed:.1f}")

            if not args.apply:
                conn.rollback()
                print("")
                print("DRY RUN ONLY. No database changes committed.")
                return 0

            input_json = {
                "application_id": application_id,
                "company": app.get("company"),
                "job_title": app.get("job_title"),
                "job_url": app.get("job_url"),
                "jd_hash": app.get("jd_hash"),
                "profile_context_pack_id": profile_pack_id,
                "profile_snapshot_hash": snapshot_hash,
                "analyzer_version": ANALYZER_VERSION,
            }

            component_run_id = insert_component_run(
                cur,
                application_id=application_id,
                model=args.model,
                input_json=input_json,
                output_json=analysis,
                raw_output=raw_output,
                prompt=prompt,
            )

            fit_analysis_id = upsert_fit_analysis(
                cur,
                application_id=application_id,
                component_run_id=component_run_id,
                profile_context_pack_id=profile_pack_id,
                model=args.model,
                analysis=analysis,
                raw_output=raw_output,
            )

            update_application(cur, application_id=application_id, analysis=analysis)

            conn.commit()

            print("")
            print("===== SAVED =====")
            print(f"component_run_id:  {component_run_id}")
            print(f"fit_analysis_id:   {fit_analysis_id}")
            print(f"application_id:    {application_id}")
            print(f"fit_score:         {analysis['fit_score']}")
            print(f"fit_decision:      {analysis['fit_decision']}")
            print(f"priority:          {analysis['priority']}")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
