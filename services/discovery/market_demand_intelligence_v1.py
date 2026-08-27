#!/usr/bin/env python3
"""LLM-first, source-grounded market-requirement extraction for captured JDs.

The SQL queue is intentionally independent of application outcome: a JD that
is filtered out or fit-rejected remains market evidence. No technology-name
allowlist is used. Every saved observation needs an LLM-supplied quote which
the deterministic validator locates in the original JD.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from services.common.llm_gateway import LLMGatewayError, chat_text, resolve_config
from services.common.config import database_dsn

EXTRACTOR_VERSION = "market_requirement_llm_v2_2026_08_20"
MAX_CHUNK_CHARS, CHUNK_OVERLAP_CHARS = 9000, 650
ALLOWED_CATEGORIES = {
    "tool", "framework", "language", "platform", "cloud_service", "database",
    "technical_skill", "technical_method", "security_standard", "certification",
    "domain_knowledge", "education_requirement", "work_authorization",
    "soft_skill", "responsibility", "other_requirement",
}
ALLOWED_IMPORTANCE = {"required", "preferred", "mentioned", "unknown"}


def normalize_keyword(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def clean_json_object(raw: str) -> dict[str, Any]:
    """Parse JSON from a normal response or a Markdown-fenced response."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.I | re.S).strip()
    fence_token = chr(96) * 3
    cleaned = cleaned.replace(f"{fence_token}json", fence_token).replace(f"{fence_token}JSON", fence_token)
    fenced = re.search(
        re.escape(fence_token) + r"\s*(.*?)\s*" + re.escape(fence_token), cleaned, flags=re.S
    )
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            parsed = None
        else:
            if not isinstance(parsed, dict):
                raise ValueError("Model response JSON must be an object.")
            return parsed
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None
    else:
        if not isinstance(parsed, dict):
            raise ValueError("Model response JSON must be an object.")
        return parsed
    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first >= 0 and last > first:
        try:
            parsed = json.loads(cleaned[first:last + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Model response did not contain a JSON object.")


def chunk_jd(jd_text: str, *, size: int = MAX_CHUNK_CHARS,
             overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Split long JDs at nearby natural boundaries while retaining context."""
    text = (jd_text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start + size // 2, end),
                           text.rfind(". ", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def extraction_prompt(chunk: str, number: int, total: int) -> str:
    categories = ", ".join(sorted(ALLOWED_CATEGORIES))
    return f"""Read this job-description chunk and return ONLY a JSON object.
Extract every explicit requirement useful for learning/project planning,
including unfamiliar tools: software, frameworks, languages, platforms,
clouds, databases, security products, standards, methods, certifications,
education/authorization requirements, technical/domain skills, and concrete
responsibilities. Do not use a pre-defined technology list. Do not infer.

JSON shape:
{{"requirements":[{{"name":"short name used in the JD","category":"{categories}","importance":"required|preferred|mentioned|unknown","evidence_quote":"short verbatim quote"}}]}}

Each item needs its own exact quote from this chunk. Include poor-fit jobs too.
No explanations or chain-of-thought.

Chunk {number}/{total}:
---
{chunk}
---"""


def locate_exact_quote(source: str, quote: str) -> str | None:
    """Return a literal source slice for a whitespace-tolerant model quote."""
    words = re.findall(r"\S+", quote or "")
    if not words:
        return None
    found = re.search(r"\s+".join(re.escape(word) for word in words), source or "", flags=re.I)
    return found.group(0).strip() if found else None


def validated_requirements(jd_text: str, raw: Iterable[Any]) -> list[dict[str, str]]:
    """Enforce the evidence boundary and remove overlap duplicates."""
    rows: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name") or "").strip())[:300]
        category = normalize_keyword(str(item.get("category") or "")).replace(" ", "_")
        importance = normalize_keyword(str(item.get("importance") or "unknown"))
        evidence = locate_exact_quote(jd_text, str(item.get("evidence_quote") or ""))
        if not name or not evidence:
            continue
        category = category if category in ALLOWED_CATEGORIES else "other_requirement"
        importance = importance if importance in ALLOWED_IMPORTANCE else "unknown"
        key = (normalize_keyword(name), category)
        old = rows.get(key)
        if old is None or len(evidence) > len(old["evidence_excerpt"]):
            rows[key] = {
                "normalized_keyword": key[0], "display_keyword": name,
                "requirement_type": category, "importance": importance,
                "evidence_excerpt": evidence,
            }
    return list(rows.values())


def extract_signals(jd_text: str, *, llm_call: Callable[..., str] = chat_text) -> list[dict[str, str]]:
    """Map JD chunks with the shared LLM gateway, then source-ground output."""
    raw: list[Any] = []
    chunks = chunk_jd(jd_text)
    for number, chunk in enumerate(chunks, start=1):
        response = llm_call(
            role="market_intelligence",
            messages=[{"role": "user", "content": extraction_prompt(chunk, number, len(chunks))}],
            json_mode=True, timeout=300, temperature=0.0,
        )
        requirements = clean_json_object(response).get("requirements", [])
        if isinstance(requirements, list):
            raw.extend(requirements)
    return validated_requirements(jd_text, raw)


def load_candidates(cur, args: argparse.Namespace) -> list[tuple[str, str]]:
    states = ["pending"] + (["failed"] if args.retry_failed else [])
    cur.execute(
        """UPDATE market_requirement_extraction_runs SET status = 'pending', updated_at = now(),
             last_error = COALESCE(last_error, 'Recovered stale worker lease.')
           WHERE status = 'running'
             AND started_at < now() - make_interval(mins => %s);""",
        (args.recover_running_after_minutes,),
    )
    sql = """SELECT r.application_id::text, a.jd_text
             FROM market_requirement_extraction_runs r JOIN applications a ON a.id = r.application_id
             WHERE r.source_jd_hash = a.jd_hash AND r.status = ANY(%s)
               AND a.jd_text IS NOT NULL AND length(btrim(a.jd_text)) >= 80"""
    values: list[Any] = [states]
    if args.application_id:
        sql += " AND r.application_id = %s"
        values.append(args.application_id)
    sql += " ORDER BY r.queued_at LIMIT %s"
    values.append(args.limit)
    cur.execute(sql, values)
    return [(str(row[0]), row[1]) for row in cur.fetchall()]


def mark_running(cur, application_id: str) -> None:
    cur.execute(
        """UPDATE market_requirement_extraction_runs SET status = 'running',
             attempt_count = attempt_count + 1, started_at = now(), completed_at = NULL,
             last_error = NULL, updated_at = now() WHERE application_id = %s;""",
        (application_id,),
    )


def save_success(cur, application_id: str, signals: list[dict[str, str]],
                 backend: str, model_name: str) -> None:
    cur.execute("DELETE FROM market_requirement_signals WHERE application_id = %s AND extraction_method = 'llm_jd_pipeline';", (application_id,))
    for signal in signals:
        cur.execute(
            """INSERT INTO market_requirement_signals
                 (application_id, role_family, normalized_keyword, display_keyword, requirement_type,
                  importance, evidence_excerpt, extractor_version, extraction_method)
               SELECT %s,
                      COALESCE(
                        jfa.role_family,
                        NULLIF(lower(regexp_replace(a.job_title, '[^a-zA-Z0-9]+', '_', 'g')), ''),
                        'other'
                      ),
                      %s, %s, %s, %s, %s, %s, 'llm_jd_pipeline'
               FROM applications a LEFT JOIN LATERAL (
                 SELECT role_family FROM job_fit_analyses WHERE application_id = a.id
                 ORDER BY created_at DESC LIMIT 1
               ) jfa ON true WHERE a.id = %s
               ON CONFLICT (application_id, normalized_keyword, requirement_type) DO UPDATE
               SET display_keyword = EXCLUDED.display_keyword, importance = EXCLUDED.importance,
                   evidence_excerpt = EXCLUDED.evidence_excerpt, extractor_version = EXCLUDED.extractor_version,
                   extraction_method = EXCLUDED.extraction_method, updated_at = now();""",
            (application_id, signal["normalized_keyword"], signal["display_keyword"],
             signal["requirement_type"], signal["importance"], signal["evidence_excerpt"],
             EXTRACTOR_VERSION, application_id),
        )
    cur.execute(
        """UPDATE market_requirement_extraction_runs SET status = 'succeeded', signal_count = %s,
             extractor_version = %s, backend = %s, model_name = %s, completed_at = now(), updated_at = now()
           WHERE application_id = %s;""",
        (len(signals), EXTRACTOR_VERSION, backend, model_name, application_id),
    )


def save_failure(cur, application_id: str, error: Exception) -> None:
    cur.execute(
        """UPDATE market_requirement_extraction_runs SET status = 'failed', last_error = %s,
             completed_at = now(), updated_at = now() WHERE application_id = %s;""",
        (f"{type(error).__name__}: {error}"[:2000], application_id),
    )


def cmd_process(args: argparse.Namespace) -> int:
    """Process the queue independently of active/terminal application state."""
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            candidates = load_candidates(cur, args)
        conn.commit()
        if not candidates or not args.apply:
            print(json.dumps({"queued": len(candidates), "application_ids": [x[0] for x in candidates], "apply": args.apply}, indent=2))
            return 0
        try:
            config = resolve_config(role="market_intelligence")
        except LLMGatewayError as error:
            with conn.cursor() as cur:
                for application_id, _ in candidates:
                    mark_running(cur, application_id)
                    save_failure(cur, application_id, error)
            conn.commit()
            print(json.dumps({"queued": len(candidates), "processed": 0, "failed": len(candidates), "error": str(error)}, indent=2))
            return 2
        processed = failed = 0
        for application_id, jd_text in candidates:
            with conn.cursor() as cur:
                mark_running(cur, application_id)
            conn.commit()
            try:
                signals = extract_signals(jd_text)
                with conn.cursor() as cur:
                    save_success(cur, application_id, signals, config.backend, config.model)
                conn.commit()
                processed += 1
            except Exception as error:
                with conn.cursor() as cur:
                    save_failure(cur, application_id, error)
                conn.commit()
                failed += 1
        print(json.dumps({"queued": len(candidates), "processed": processed, "failed": failed, "apply": True}, indent=2))
        return 0 if not failed else 2


def print_rows(cur, view: str, args: argparse.Namespace) -> int:
    cur.execute(
        f"""SELECT role_family, normalized_keyword, display_keyword, requirement_type,
                    posting_count, company_count, companies, last_seen_at FROM {view}
             WHERE (%s IS NULL OR role_family = %s)
             ORDER BY posting_count DESC, company_count DESC, normalized_keyword LIMIT %s;""",
        (args.role_family, args.role_family, args.limit),
    )
    print(json.dumps([
        {"role_family": r[0], "keyword": r[1], "display_keyword": r[2], "type": r[3],
         "posting_count": r[4], "company_count": r[5], "companies": r[6] or [], "last_seen_at": str(r[7])}
        for r in cur.fetchall()
    ], indent=2))
    return 0


def cmd_ideas(cur, args: argparse.Namespace) -> int:
    cur.execute(
        """SELECT role_family, normalized_keyword, display_keyword FROM v_market_skill_gaps
             WHERE (%s IS NULL OR role_family = %s)
             ORDER BY posting_count DESC, company_count DESC, normalized_keyword LIMIT %s;""",
        (args.role_family, args.role_family, args.limit),
    )
    ideas = []
    for role, keyword, display in cur.fetchall():
        idea = {"role_family": role, "keyword": keyword,
                "title": f"{display} evidence demonstrator",
                "learning_scope": f"Build a small, documented {role} project that deliberately exercises {display}.",
                "evidence_goal": "Publish a reproducible README, architecture/decision notes, tests or validation output, and honest limitations."}
        ideas.append(idea)
        if args.apply:
            cur.execute(
                """INSERT INTO market_project_ideas (role_family, normalized_keyword, title, learning_scope, evidence_goal)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (role_family, normalized_keyword) DO UPDATE
                   SET title = EXCLUDED.title, learning_scope = EXCLUDED.learning_scope,
                       evidence_goal = EXCLUDED.evidence_goal, updated_at = now();""",
                (role, keyword, idea["title"], idea["learning_scope"], idea["evidence_goal"]),
            )
    print(json.dumps({"ideas": ideas, "apply": args.apply}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS LLM market-demand intelligence")
    subs = parser.add_subparsers(dest="command", required=True)
    process = subs.add_parser("process", help="Process every queued JD, including discarded jobs.")
    process.add_argument("--application-id")
    process.add_argument("--limit", type=int, default=20)
    process.add_argument("--retry-failed", action="store_true")
    process.add_argument("--recover-running-after-minutes", type=int, default=30)
    process.add_argument("--apply", action="store_true")
    for name, help_text in (("demands", "Show observed requirements by role/company."),
                            ("gaps", "Show observed requirements absent from approved profile terms."),
                            ("ideas", "Create reviewable project-learning ideas from observed gaps.")):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("--role-family")
        sub.add_argument("--limit", type=int, default=30)
        if name == "ideas":
            sub.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "process":
        return cmd_process(args)
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            code = cmd_ideas(cur, args) if args.command == "ideas" else print_rows(
                cur, "v_market_keyword_demands" if args.command == "demands" else "v_market_skill_gaps", args)
        (conn.commit() if getattr(args, "apply", False) else conn.rollback())
        return code


if __name__ == "__main__":
    raise SystemExit(main())
