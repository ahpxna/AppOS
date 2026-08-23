#!/usr/bin/env python3
"""L0/L1 -- profile-driven job-search planning and transparent ranking.

This script does not scrape LinkedIn or submit applications.  It reads only
approved profile capabilities, produces human-operated LinkedIn search links,
and ranks already-intaked `applications` before L5 makes an evidence-grounded
fit assessment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.profile_job_matching import rank_job, unique_terms
from services.common.config import database_dsn

DSN = database_dsn()


def approved_terms(cur) -> list[str]:
    """Read the approved-only terms view; drafts are intentionally excluded."""
    cur.execute("SELECT term FROM v_profile_search_terms ORDER BY term;")
    return [row[0] for row in cur.fetchall()]


def cmd_terms(cur, _args) -> int:
    terms = approved_terms(cur)
    print(json.dumps({"approved_profile_terms": terms, "count": len(terms)}, indent=2))
    return 0


def cmd_queries(cur, args) -> int:
    """Print manual LinkedIn search links; this command performs no scraping."""
    terms = unique_terms([*approved_terms(cur), *args.keyword])
    if not terms:
        print("No approved profile terms. Approve capabilities before generating searches.")
        return 1
    # One term per URL stays reviewable; this is a manual search aid, not a crawler.
    queries = [
        {
            "term": term,
            "linkedin_url": "https://www.linkedin.com/jobs/search/?" + urlencode({"keywords": term}),
            "instruction": "Open yourself, review results, then import only jobs you want analysed.",
        }
        for term in terms[:args.limit]
    ]
    print(json.dumps({"mode": "human_operated_linkedin_search", "queries": queries}, indent=2))
    return 0


def cmd_rank(cur, args) -> int:
    """Rank already-intaked jobs with transparent profile-term overlap."""
    terms = approved_terms(cur)
    cur.execute(
        """
        SELECT a.id::text, a.source, a.company, a.job_title, a.job_url, a.jd_text,
               a.current_step, a.status, a.location, a.work_mode,
               COALESCE(ia.status, 'UNKNOWN'),
               COALESCE(ia.restriction_type, 'UNKNOWN'),
               COALESCE(ia.jd_policy_result, 'unknown'),
               COALESCE(ia.jd_policy_evidence, '[]'::jsonb),
               COALESCE(ia.everify_status, 'unknown'),
               COALESCE(ia.h1b_history_status, 'unknown'),
               ia.final_reason
        FROM applications a
        LEFT JOIN application_immigration_assessments ia ON ia.application_id = a.id
        WHERE (%s OR status = 'active')
        ORDER BY a.created_at DESC;
        """,
        (args.include_inactive,),
    )
    results = []
    for row in cur.fetchall():
        match = rank_job(title=row[3] or "", jd_text=row[5] or "",
                         profile_terms=terms, user_keywords=args.keyword)
        if match["discovery_score"] < args.min_score:
            continue
        immigration = {
            "status": row[10], "restriction_type": row[11], "jd_policy": row[12],
            "jd_policy_evidence": row[13] or [], "everify": row[14],
            "h1b_history": row[15], "reason": row[16] or "",
        }
        if args.exclude_immigration_blocked and immigration["status"] == "BLOCKED":
            continue
        results.append({
            "application_id": row[0], "source": row[1], "company": row[2],
            "job_title": row[3], "job_url": row[4], "current_step": row[6],
            "status": row[7], "location": row[8], "work_mode": row[9],
            "immigration_fit": immigration, **match,
        })
    immigration_order = {"HIGH": 0, "POSSIBLE": 1, "UNKNOWN": 2, "LOW": 3, "BLOCKED": 4}
    results.sort(key=lambda item: (
        immigration_order.get(item["immigration_fit"]["status"], 5),
        -item["discovery_score"], item["company"] or "", item["job_title"] or "",
    ))
    print(json.dumps({"profile_term_count": len(terms), "matches": results[:args.limit]}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile-driven JobOS discovery")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("terms", help="List only approved capability terms.")
    queries = subs.add_parser("queries", help="Make human-operated LinkedIn job-search URLs; no network request is made.")
    rank = subs.add_parser("rank", help="Rank intaked applications by profile-term and keyword overlap.")
    for sub in (queries, rank):
        sub.add_argument("--keyword", action="append", default=[], help="Extra user search keyword; repeatable.")
        sub.add_argument("--limit", type=int, default=25)
    rank.add_argument("--min-score", type=int, default=1)
    rank.add_argument("--include-inactive", action="store_true")
    rank.add_argument("--exclude-immigration-blocked", action="store_true",
                      help="Hide only JDs with an explicit incompatible immigration policy; unknown remains visible.")
    args = parser.parse_args()
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            return {"terms": cmd_terms, "queries": cmd_queries, "rank": cmd_rank}[args.command](cur, args)


if __name__ == "__main__":
    raise SystemExit(main())
