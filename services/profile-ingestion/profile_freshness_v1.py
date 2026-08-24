#!/usr/bin/env python3
"""Read-only aggregate status for versioned profile sources and resume freshness."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.common.profile_freshness import assess_resume_profile, explain_blockers


def _load_db():
    import psycopg
    from services.common.config import database_dsn
    return psycopg, database_dsn


def status() -> dict[str, Any]:
    psycopg, database_dsn = _load_db()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        report = assess_resume_profile(cur)
        cur.execute(
            """
            SELECT logical_source_key, display_name, source_kind, authority_class,
                   status, content_sha256, embedded_created_at, embedded_modified_at,
                   filesystem_modified_at, ingested_at
            FROM v_profile_source_freshness ORDER BY logical_source_key;
            """
        )
        sources = [
            {"logical_source_key": r[0], "display_name": r[1], "source_kind": r[2], "authority_class": r[3],
             "status": r[4], "content_sha256": r[5], "embedded_created_at": r[6],
             "embedded_modified_at": r[7], "filesystem_modified_at": r[8], "ingested_at": r[9]}
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT project_id, claim_key, github_value, document_value, resolution,
                   status, resolution_note
            FROM project_source_conflicts ORDER BY status, project_id, claim_key;
            """
        )
        conflicts = [
            {"project_id": r[0], "claim_key": r[1], "github_value": r[2], "document_value": r[3],
             "resolution": r[4], "status": r[5], "resolution_note": r[6]}
            for r in cur.fetchall()
        ]
    return {"resume": report, "blockers": explain_blockers(report), "source_documents": sources, "conflicts": conflicts}


def main() -> int:
    parser = argparse.ArgumentParser(description="Show current profile/source freshness.")
    parser.add_argument("command", choices=("status", "conflicts"), default="status", nargs="?")
    args = parser.parse_args()
    payload = status()
    if args.command == "conflicts":
        payload = {"conflicts": payload["conflicts"]}
    print(json.dumps(payload, indent=2, default=str))
    return 0 if args.command == "conflicts" or payload["resume"]["resume_profile_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
