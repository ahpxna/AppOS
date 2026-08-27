#!/usr/bin/env python3
"""Prepare deterministic profile briefs and base context packs after review.

This is the explicit bridge between human-approved profile evidence and the
L5/L6 pipeline. It never approves assets or capabilities. Once those review
gates are satisfied, ``build --apply`` runs the two existing deterministic SQL
builders in one transaction and makes the base packs available to job fit,
resume, cover letter, interview, and message stages.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg


REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(REPO_ROOT))
from services.common.config import database_dsn
BRIEFS_SQL = REPO_ROOT / "services" / "profile-ingestion" / "generate_profile_briefs_v1.sql"
PACKS_SQL = REPO_ROOT / "services" / "profile-ingestion" / "build_profile_context_packs_v1.sql"
BASE_PACK_PURPOSES = (
    "base_fit_check_support",
    "base_resume_generation",
    "base_cover_letter_generation",
    "base_short_answer_generation",
    "base_interview_prep",
    "base_message_reply",
)

def profile_state(cur) -> dict[str, Any]:
    """Read only the approval and pack state needed by deterministic builders."""
    cur.execute("SELECT count(*) FROM profile_assets WHERE status = 'approved';")
    assets = int(cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM profile_capabilities WHERE status = 'approved';")
    capabilities = int(cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM profile_briefs WHERE is_stale = false;")
    briefs = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT purpose FROM profile_context_packs
        WHERE application_id IS NULL AND message_thread_id IS NULL
          AND purpose = ANY(%s);
        """,
        (list(BASE_PACK_PURPOSES),),
    )
    present = {row[0] for row in cur.fetchall()}
    return {
        "approved_assets": assets,
        "approved_capabilities": capabilities,
        "fresh_briefs": briefs,
        "present_base_packs": sorted(present),
        "missing_base_packs": [purpose for purpose in BASE_PACK_PURPOSES if purpose not in present],
    }


def validate_build_inputs(state: dict[str, Any]) -> list[str]:
    """Keep review gates human-owned before running SQL that consumes them."""
    missing = []
    if state["approved_assets"] == 0:
        missing.append("approved profile assets")
    if state["approved_capabilities"] == 0:
        missing.append("approved profile capabilities")
    return missing


def execute_builder(cur, sql_path: Path) -> None:
    """Execute one tracked deterministic builder inside the caller transaction.

    The legacy SQL files are also runnable directly through ``psql`` and have
    outer ``BEGIN``/``COMMIT`` wrappers. Strip only those file-level wrappers
    here so a failed second builder can roll back the first builder too.
    """
    if not sql_path.exists():
        raise RuntimeError(f"Builder SQL not found: {sql_path}")
    cur.execute(strip_outer_transaction(sql_path.read_text(encoding="utf-8"), sql_path))


def strip_outer_transaction(sql: str, sql_path: Path) -> str:
    """Remove only a top-level ``BEGIN; … COMMIT;`` pair from tracked SQL."""
    lines = sql.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("--")), None)
    last = next((index for index in range(len(lines) - 1, -1, -1)
                 if lines[index].strip() and not lines[index].lstrip().startswith("--")), None)
    if first is None or last is None:
        raise RuntimeError(f"Builder SQL is empty: {sql_path}")
    if lines[first].strip().upper() != "BEGIN;" or lines[last].strip().upper() != "COMMIT;":
        raise RuntimeError(f"Builder SQL must use one outer BEGIN/COMMIT pair: {sql_path}")
    del lines[last]
    del lines[first]
    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare approved JobOS profile evidence for L5/L6.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show readiness without writing.")
    build = sub.add_parser("build", help="Build briefs and base packs from approved evidence.")
    build.add_argument("--apply", action="store_true", help="Commit; otherwise only report what is missing.")
    args = parser.parse_args()

    try:
        with psycopg.connect(database_dsn(), autocommit=False) as conn:
            with conn.cursor() as cur:
                before = profile_state(cur)
                missing = validate_build_inputs(before)
                if args.command == "status":
                    conn.rollback()
                    print(json.dumps({"writes": False, "state": before, "build_blockers": missing}, indent=2))
                    return 0 if not missing else 2
                if missing:
                    conn.rollback()
                    print(json.dumps({
                        "writes": False,
                        "state": before,
                        "error": "Cannot build context packs without human-approved evidence.",
                        "build_blockers": missing,
                    }, indent=2))
                    return 2
                if not args.apply:
                    conn.rollback()
                    print(json.dumps({
                        "writes": False,
                        "state": before,
                        "next": "Re-run build --apply to execute the deterministic brief and pack builders.",
                    }, indent=2))
                    return 0
                execute_builder(cur, BRIEFS_SQL)
                execute_builder(cur, PACKS_SQL)
                after = profile_state(cur)
                if after["missing_base_packs"]:
                    raise RuntimeError("Builders completed without every required base context pack.")
                conn.commit()
                print(json.dumps({"writes": True, "before": before, "after": after}, indent=2))
                return 0
    except (psycopg.Error, RuntimeError) as exc:
        print(json.dumps({"writes": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
