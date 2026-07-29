"""
L5 -- COMPANY RESEARCH WRITER

Fills company_research_cache using OpenClaw's web fetch/search tools.

Transport note: this deliberately uses the agent's *webfetch/websearch* tools,
NOT the browser tool. The browser tool needs a live CDP connection (socat ->
container port -> chrome started on that port -> profile="remote" passed
explicitly, because browser.defaultProfile does not propagate into tool calls).
Public company pages do not need any of that. Keep the browser tool for
authenticated pages only.

Cache policy:
  A hit that has not expired is returned untouched. Research is the cheapest
  thing in the pipeline to get wrong and the most annoying to re-fetch, so the
  default TTL is deliberately long (30 days) and refresh is explicit.

Grounding:
  Every field must cite a source URL that actually appeared in the fetched
  material. Claims without a source are dropped before the row is written, the
  same rule L6 applies to profile assets. A company summary that quietly
  invents a funding round is worse than an empty cache row.

Usage:
  python services/research/company_research_v1.py --company "MSSP Co"
  python services/research/company_research_v1.py --company "Acme" --domain acme.com --apply
  python services/research/company_research_v1.py --for-application <uuid> --apply
  python services/research/company_research_v1.py --list-stale
"""

from __future__ import annotations
import uuid

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT_RESEARCH", "main")

RESEARCH_VERSION = "company_research_v1_webfetch_2026_07_28"
DEFAULT_TTL_DAYS = int(os.getenv("JOBOS_RESEARCH_TTL_DAYS", "30"))


# ---------------------------------------------------------------- openclaw

def openclaw_agent(message: str, *, timeout: int) -> str:
    if shutil.which(OPENCLAW_BIN) is None:
        raise RuntimeError(f"'{OPENCLAW_BIN}' not on PATH.")

    # A fresh session per research run: avoids colliding with agent:main:global
    # and keeps one company's context out of the next company's answer.
    session_id = f"jobos-research-{uuid.uuid4().hex[:12]}"

    cmd = [
        OPENCLAW_BIN, "agent",
        "--agent", OPENCLAW_AGENT,
        "--message", message,
        "--json",
        "--timeout", str(timeout),
        "--session-id", session_id,
    ]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout + 60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise RuntimeError(f"openclaw agent failed: {err}")
    return proc.stdout


def unwrap_agent_payload(raw: str) -> str:
    """`openclaw agent --json` returns an envelope:
      {runId, status, summary:"completed", result:{payloads:[{text}], meta:{...}}}
    The envelope has its own 'summary' field, so it must be detected and
    unwrapped BEFORE looking for research fields, or 'completed' gets mistaken
    for the company summary."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)

    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first == -1 or last <= first:
        return cleaned

    try:
        env = json.loads(cleaned[first:last + 1])
    except json.JSONDecodeError:
        return cleaned

    if not isinstance(env, dict) or "result" not in env:
        return cleaned

    if env.get("status") not in (None, "ok"):
        raise RuntimeError(f"Agent run status={env.get('status')!r}")

    result = env.get("result") or {}

    payloads = result.get("payloads") or []
    if payloads and isinstance(payloads[0], dict) and payloads[0].get("text"):
        return payloads[0]["text"]

    meta = result.get("meta") or {}
    for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
        if meta.get(key):
            return meta[key]

    return cleaned


def extract_json_object(raw: str) -> Dict[str, Any]:
    inner = unwrap_agent_payload(raw)

    inner = inner.replace("```json", "```").replace("```JSON", "```").strip()
    fence = re.search(r"```(.*?)```", inner, flags=re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    first, last = inner.find("{"), inner.rfind("}")
    if first == -1 or last <= first:
        raise ValueError(
            "Agent replied without a JSON object. Reply began: "
            + inner.strip()[:200]
        )
    return json.loads(inner[first:last + 1])


# ---------------------------------------------------------------- prompt

def build_research_prompt(company: str, domain: Optional[str]) -> str:
    target = f"{company}" + (f" (website: {domain})" if domain else "")
    return f"""Research this company for a job applicant: {target}

Use your web search and web fetch tools. Do NOT use the browser tool; these are
public pages and do not need a browser session.

Gather:
1. What the company actually does, in plain language.
2. Its stated mission or values, if published.
3. Its main products or services.
4. Any notable recent news from the last 12 months.
5. Anything a candidate should weigh before applying: layoffs, lawsuits,
   acquisitions, leadership churn, sustained poor employee reviews.

Rules:
- Every claim must come from a page you actually fetched. Record its URL.
- If you cannot find something, use an empty string or empty list. Do not guess.
- Do not infer headcount, revenue, or funding unless a fetched page states it.
- For the risks field, report what sources say. Do not soften it and do not
  invent concerns that no source raised.
- Prefer the company's own site, then reputable news, then review sites.

Return ONLY a JSON object, no prose before or after:
{{
  "company_domain": "primary domain, or empty string",
  "summary": "3-4 sentences on what they do",
  "mission": "stated mission, or empty string",
  "products": "main products or services",
  "recent_news": [
    {{"headline": "...", "date": "YYYY-MM or YYYY-MM-DD", "source_url": "..."}}
  ],
  "risks": [
    {{"risk": "...", "detail": "one sentence", "source_url": "..."}}
  ],
  "sources": ["every URL you fetched"],
  "not_found": ["fields you could not source"]
}}
"""


# ---------------------------------------------------------------- validation

def validate(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Drop any news item or risk that lacks a source URL, self-sources, or lacks a quote."""
    sources = [s for s in (parsed.get("sources") or []) if isinstance(s, str) and s.startswith("http")]
    source_set = set(sources)

    kept_news, dropped = [], []
    for item in parsed.get("recent_news") or []:
        url = (item or {}).get("source_url", "")
        if url in source_set:
            kept_news.append(item)
        else:
            dropped.append(f"news: {str(item)[:80]}")

    company_domain = (parsed.get("company_domain") or "").lower()
    kept_risks = []
    
    for item in parsed.get("risks") or []:
        url = (item or {}).get("source_url", "")
        
        # 1. Kiểm tra có nằm trong danh sách nguồn được fetch không
        if url not in source_set:
            dropped.append(f"risk (unsourced): {str(item)[:80]}")
            continue
            
        # 2. Chặn self-sourcing (Trang chủ công ty không được làm nguồn tố cáo chính nó)
        if company_domain and company_domain in url:
            dropped.append(f"risk (self-sourced to {company_domain}): {item.get('risk','')}")
            continue
            
        # 3. Bắt buộc phải có trích dẫn nguyên văn từ nguồn
        if not (item.get("supporting_quote") or "").strip():
            dropped.append(f"risk (no quote): {item.get('risk','')}")
            continue
            
        kept_risks.append(item)

    return {
        "company_domain": (parsed.get("company_domain") or "").strip(),
        "summary": (parsed.get("summary") or "").strip(),
        "mission": (parsed.get("mission") or "").strip(),
        "products": (parsed.get("products") or "").strip(),
        "recent_news": kept_news,
        "risks": kept_risks,
        "sources": sources,
        "not_found": parsed.get("not_found") or [],
        "dropped_unsourced": dropped,
        "research_version": RESEARCH_VERSION,
    }


# ---------------------------------------------------------------- data access

def get_cached(cur, company: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT id::text, company_domain, summary, last_refreshed_at, expires_at,
               (expires_at IS NOT NULL AND expires_at > now()) AS fresh
        FROM company_research_cache
        WHERE lower(company_name) = lower(%s)
        ORDER BY last_refreshed_at DESC NULLS LAST
        LIMIT 1;
        """,
        (company,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "domain": r[1], "summary": r[2],
            "refreshed": r[3], "expires": r[4], "fresh": r[5]}


def upsert_research(cur, company: str, data: Dict[str, Any], ttl_days: int) -> str:
    cur.execute(
        "DELETE FROM company_research_cache WHERE lower(company_name) = lower(%s);",
        (company,),
    )
    cur.execute(
        """
        INSERT INTO company_research_cache
          (company_name, company_domain, summary, mission, products,
           recent_news, risks, sources, last_refreshed_at, expires_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                now(), now() + make_interval(days => %s), now())
        RETURNING id::text;
        """,
        (
            company, data["company_domain"] or None, data["summary"],
            data["mission"], data["products"],
            Jsonb(data["recent_news"]), Jsonb(data["risks"]),
            Jsonb({"urls": data["sources"],
                   "not_found": data["not_found"],
                   "dropped_unsourced": data["dropped_unsourced"],
                   "research_version": RESEARCH_VERSION}),
            ttl_days,
        ),
    )
    return cur.fetchone()[0]


def company_for_application(cur, application_id: str) -> tuple:
    cur.execute(
        "SELECT company, job_url FROM applications WHERE id = %s;", (application_id,)
    )
    r = cur.fetchone()
    if not r:
        raise RuntimeError(f"Application not found: {application_id}")
    domain = None
    if r[1]:
        m = re.search(r"https?://([^/]+)", r[1])
        if m:
            domain = m.group(1)
    return r[0], domain


# ---------------------------------------------------------------- main

def research_company(conn, company: str, domain: Optional[str], args) -> int:
    with conn.cursor() as cur:
        cached = get_cached(cur, company)
        if cached and cached["fresh"] and not args.force:
            print(f"  cache hit, fresh until {cached['expires']}")
            print(f"  {cached['summary'][:200]}")
            print("  use --force to refresh anyway")
            return 0
        if cached:
            print(f"  cache stale (expired {cached['expires']}); refreshing")

        prompt = build_research_prompt(company, domain)
        print(f"  querying agent '{OPENCLAW_AGENT}' via web fetch/search...")

        start = time.time()
        session_id = f"jobos-research-{_uuid.uuid4().hex[:12]}"
        raw = openclaw_agent(prompt, timeout=args.timeout, session_id=session_id)
        elapsed = time.time() - start

        parsed = extract_json_object(raw)
        data = validate(parsed)

        print(f"\n  elapsed:  {elapsed:.1f}s")
        print(f"  domain:   {data['company_domain'] or '(none)'}")
        print(f"  summary:  {data['summary'][:220]}")
        print(f"  news:     {len(data['recent_news'])}")
        print(f"  risks:    {len(data['risks'])}")
        print(f"  sources:  {len(data['sources'])}")
        if data["dropped_unsourced"]:
            print(f"  dropped {len(data['dropped_unsourced'])} unsourced item(s):")
            for d in data["dropped_unsourced"]:
                print(f"    - {d}")
        if data["not_found"]:
            print(f"  not found: {', '.join(data['not_found'])}")

        for r in data["risks"]:
            print(f"    RISK: {r.get('risk')} -- {r.get('source_url','')}")

        if not data["sources"]:
            print("\n  No sources were fetched. Not writing an unsourced row.")
            conn.rollback()
            return 1

        if not args.apply:
            conn.rollback()
            print("\nDRY RUN. Nothing committed.")
            return 0

        row_id = upsert_research(cur, company, data, args.ttl_days)
        conn.commit()
        print(f"\n  saved: {row_id} (expires in {args.ttl_days} days)")
        return 0


def cmd_list_stale(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.company_name, c.last_refreshed_at, c.expires_at,
                   jsonb_array_length(COALESCE(c.sources->'urls', '[]'::jsonb))
            FROM company_research_cache c
            WHERE c.expires_at IS NULL OR c.expires_at <= now()
            ORDER BY c.last_refreshed_at NULLS FIRST;
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("Nothing stale.")
            return 0
        print(f"\n{'COMPANY':<32} {'REFRESHED':<22} SOURCES")
        print("-" * 64)
        for name, refreshed, _exp, nsrc in rows:
            print(f"{name:<32} {str(refreshed)[:19]:<22} {nsrc}")

        cur.execute(
            """
            SELECT DISTINCT a.company
            FROM applications a
            LEFT JOIN company_research_cache c
              ON lower(c.company_name) = lower(a.company)
            WHERE c.id IS NULL AND a.company IS NOT NULL
            ORDER BY a.company;
            """
        )
        missing = [r[0] for r in cur.fetchall()]
        if missing:
            print(f"\nApplications with no research at all: {', '.join(missing)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L5 company research writer")
    p.add_argument("--company")
    p.add_argument("--domain")
    p.add_argument("--for-application", dest="for_application")
    p.add_argument("--list-stale", action="store_true")
    p.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--force", action="store_true", help="Refresh even if fresh.")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    print(f"===== COMPANY RESEARCH ({RESEARCH_VERSION}) =====")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")

    with psycopg.connect(DSN, autocommit=False) as conn:
        if args.list_stale:
            return cmd_list_stale(conn)

        if args.for_application:
            with conn.cursor() as cur:
                company, domain = company_for_application(cur, args.for_application)
        elif args.company:
            company, domain = args.company, args.domain
        else:
            print("ERROR: need --company, --for-application, or --list-stale.")
            return 2

        if not company:
            print("ERROR: no company name available.")
            return 1

        print(f"Company: {company}\n")
        try:
            return research_company(conn, company, domain, args)
        except (RuntimeError, ValueError) as e:
            conn.rollback()
            print(f"\nERROR: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
