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

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")
DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"


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
        SELECT id::text, source, company, job_title, job_url, jd_text,
               current_step, status, location, work_mode
        FROM applications
        WHERE (%s OR status = 'active')
        ORDER BY created_at DESC;
        """,
        (args.include_inactive,),
    )
    results = []
    for row in cur.fetchall():
        match = rank_job(title=row[3] or "", jd_text=row[5] or "",
                         profile_terms=terms, user_keywords=args.keyword)
        if match["discovery_score"] < args.min_score:
            continue
        results.append({
            "application_id": row[0], "source": row[1], "company": row[2],
            "job_title": row[3], "job_url": row[4], "current_step": row[6],
            "status": row[7], "location": row[8], "work_mode": row[9], **match,
        })
    results.sort(key=lambda item: (-item["discovery_score"], item["company"] or "", item["job_title"] or ""))
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
    args = parser.parse_args()
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            return {"terms": cmd_terms, "queries": cmd_queries, "rank": cmd_rank}[args.command](cur, args)


if __name__ == "__main__":
    raise SystemExit(main())
