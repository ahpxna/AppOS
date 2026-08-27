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

# JOBOS_DIRECT_FILE_BOOTSTRAP: keep direct `python path/to/file.py` usable
# while package imports resolve exactly as they do under `python -m ...`.
import sys as _jobos_sys
from pathlib import Path as _JobOSPath
_JOBOS_ROOT = _JobOSPath(__file__).resolve().parents[2]
if str(_JOBOS_ROOT) not in _jobos_sys.path:
    _jobos_sys.path.insert(0, str(_JOBOS_ROOT))
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

from services.common.observability import emit_trace, make_trace_id
from services.common.config import database_dsn

from services.common.openclaw_runtime import resolve_openclaw_binary

OPENCLAW_BIN = resolve_openclaw_binary()
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT_RESEARCH", "main")

RESEARCH_VERSION = "company_research_v1_webfetch_2026_07_28"
DEFAULT_TTL_DAYS = int(os.getenv("JOBOS_RESEARCH_TTL_DAYS", "30"))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


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
            parsed = json.loads(fence.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    first, last = inner.find("{"), inner.rfind("}")
    if first == -1 or last <= first:
        raise ValueError(
            "Agent replied without a JSON object. Reply began: "
            + inner.strip()[:200]
        )
    parsed = json.loads(inner[first:last + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Research output JSON must be an object.")
    return parsed


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
- For summary, mission, and products, include at least one field_evidence item
  with the exact fetched source_url and a short verbatim supporting_quote. If a
  field has no such evidence, leave that field empty.
- Every recent_news item must also include a short verbatim supporting_quote.
- If you cannot find something, use an empty string or empty list. Do not guess.
- Do not infer headcount, revenue, or funding unless a fetched page states it.
- For the risks field, report what sources say. Do not soften it and do not
  invent concerns that no source raised. Every risk must include a short
  verbatim supporting_quote from its source_url; otherwise leave it out.
- Prefer the company's own site, then reputable news, then review sites.

Return ONLY a JSON object, no prose before or after:
{{
  "company_domain": "primary domain, or empty string",
  "summary": "3-4 sentences on what they do",
  "mission": "stated mission, or empty string",
  "products": "main products or services",
  "recent_news": [
    {{"headline": "...", "date": "YYYY-MM or YYYY-MM-DD", "source_url": "...", "supporting_quote": "short verbatim quote from that source"}}
  ],
  "field_evidence": {{
    "summary": [{{"source_url": "...", "supporting_quote": "short verbatim quote supporting the summary"}}],
    "mission": [{{"source_url": "...", "supporting_quote": "short verbatim quote supporting the mission"}}],
    "products": [{{"source_url": "...", "supporting_quote": "short verbatim quote supporting the products/services"}}]
  }},
  "risks": [
    {{"risk": "...", "detail": "one sentence", "source_url": "...", "supporting_quote": "short verbatim quote from that source"}}
  ],
  "sources": ["every URL you fetched"],
  "not_found": ["fields you could not source"]
}}
"""


# ---------------------------------------------------------------- validation

def _clean_http_sources(value: Any) -> List[str]:
    items = value if isinstance(value, list) else []
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        url = str(item or "").strip()
        if not url.startswith(("https://", "http://")) or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _validated_field_evidence(
    parsed: Dict[str, Any], source_set: set[str], dropped: List[str],
) -> Dict[str, List[Dict[str, str]]]:
    raw = parsed.get("field_evidence")
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, List[Dict[str, str]]] = {}
    for field in ("summary", "mission", "products"):
        entries = raw.get(field)
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            entries = []
        kept: List[Dict[str, str]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            url = str(item.get("source_url") or "").strip()
            quote = " ".join(str(item.get("supporting_quote") or "").split())
            if url not in source_set or not quote:
                dropped.append(f"{field} evidence (unsourced/no quote): {str(item)[:80]}")
                continue
            kept.append({"source_url": url, "supporting_quote": quote})
        if kept:
            out[field] = kept
    return out


def validate(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize research and require per-claim provenance promised by the prompt.

    Source URLs alone are not evidence for a generated company fact. New
    summary/mission/products fields therefore survive only when the model also
    returns a source URL + quote binding. News and risk items use the same
    contract. Missing evidence degrades to an empty field/item instead of
    fabricating or failing the whole research run.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    sources = _clean_http_sources(parsed.get("sources"))
    source_set = set(sources)
    dropped: List[str] = []
    field_evidence = _validated_field_evidence(parsed, source_set, dropped)

    company_domain = str(parsed.get("company_domain") or "").strip().lower()

    fields: Dict[str, str] = {}
    for field in ("summary", "mission", "products"):
        value = str(parsed.get(field) or "").strip()
        if value and not field_evidence.get(field):
            dropped.append(f"{field}: generated value had no source quote binding")
            value = ""
        fields[field] = value

    kept_news: List[Dict[str, Any]] = []
    for item in parsed.get("recent_news") if isinstance(parsed.get("recent_news"), list) else []:
        if not isinstance(item, dict):
            dropped.append(f"news malformed: {str(item)[:80]}")
            continue
        url = str(item.get("source_url") or "").strip()
        quote = " ".join(str(item.get("supporting_quote") or "").split())
        if url not in source_set or not quote:
            dropped.append(f"news (unsourced/no quote): {str(item)[:80]}")
            continue
        kept_news.append({
            "headline": str(item.get("headline") or "").strip(),
            "date": str(item.get("date") or "").strip(),
            "source_url": url,
            "supporting_quote": quote,
        })

    kept_risks: List[Dict[str, Any]] = []
    for item in parsed.get("risks") if isinstance(parsed.get("risks"), list) else []:
        if not isinstance(item, dict):
            dropped.append(f"risk malformed: {str(item)[:80]}")
            continue
        url = str(item.get("source_url") or "").strip()
        if url not in source_set:
            dropped.append(f"risk (unsourced): {str(item)[:80]}")
            continue
        if company_domain and company_domain in url.lower():
            dropped.append(f"risk (self-sourced to {company_domain}): {item.get('risk','')}")
            continue
        quote = " ".join(str(item.get("supporting_quote") or "").split())
        if not quote:
            dropped.append(f"risk (no quote): {item.get('risk','')}")
            continue
        kept_risks.append({
            "risk": str(item.get("risk") or "").strip(),
            "detail": str(item.get("detail") or "").strip(),
            "source_url": url,
            "supporting_quote": quote,
        })

    raw_not_found = parsed.get("not_found")
    not_found = [str(x).strip() for x in raw_not_found if str(x).strip()] if isinstance(raw_not_found, list) else []
    return {
        "company_domain": company_domain,
        **fields,
        "recent_news": kept_news,
        "risks": kept_risks,
        "sources": sources,
        "field_evidence": field_evidence,
        "not_found": not_found,
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
                   "field_evidence": data.get("field_evidence") or {},
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

        start = time.perf_counter()
        # openclaw_agent() already mints its own per-call session id (see
        # its definition above); it does not accept a session_id kwarg.
        # This call used to pass one anyway (and referenced an undefined
        # `_uuid` name on top of that), which raised TypeError the moment
        # this path actually ran against a live OpenClaw install. Fixed
        # 2026-07-31.
        raw = openclaw_agent(prompt, timeout=args.timeout)
        elapsed = time.perf_counter() - start
        emit_trace(
            make_trace_id("company-research", company),
            "company_research",
            started_at=start,
            tokens_in=estimate_tokens(prompt),
            tokens_out=estimate_tokens(raw),
            cost_usd=0.0,
            company=company,
        )

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
                   CASE
                     WHEN jsonb_typeof(c.sources) = 'array' THEN jsonb_array_length(c.sources)
                     WHEN jsonb_typeof(c.sources) = 'object' AND jsonb_typeof(c.sources->'urls') = 'array'
                       THEN jsonb_array_length(c.sources->'urls')
                     WHEN jsonb_typeof(c.sources) = 'object' AND c.sources ? 'url' THEN 1
                     ELSE 0
                   END
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

    with psycopg.connect(database_dsn(), autocommit=False) as conn:
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
