#!/usr/bin/env python3
"""View or update typed, local discovery preferences without editing SQL."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn
from services.common.search_preferences import validate_location_pattern


FIELDS = ("company_blacklist", "title_blacklist", "location_blacklist", "location_allow_patterns",
          "allowed_work_modes", "allowed_employment_types", "freshness_days", "salary_floor",
          "max_active_applications_per_employer")


def csv_list(value: str | None) -> list[str] | None:
    return None if value is None else [part.strip() for part in value.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS typed discovery preferences")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("show")
    update = subs.add_parser("set")
    for field in FIELDS[:6]:
        update.add_argument("--" + field.replace("_", "-"))
    update.add_argument("--freshness-days", type=int)
    update.add_argument("--salary-floor", type=float)
    update.add_argument("--max-active-applications-per-employer", type=int)
    update.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        if args.command == "show":
            cur.execute("SELECT * FROM job_search_preferences WHERE profile_key = 'primary';")
            row = cur.fetchone()
            names = [column.name for column in cur.description]
            print(json.dumps(dict(zip(names, row or ())), default=str, indent=2))
            return 0
        changes = {}
        for field in FIELDS[:6]:
            value = csv_list(getattr(args, field))
            if value is not None:
                if field == "location_allow_patterns":
                    value = [validate_location_pattern(pattern) for pattern in value]
                changes[field] = value
        for field in FIELDS[6:]:
            value = getattr(args, field)
            if value is not None:
                changes[field] = value
        if not changes:
            raise SystemExit("Provide at least one preference value.")
        assignments = ", ".join(f"{name} = %s" for name in changes) + ", updated_at = now()"
        cur.execute(f"UPDATE job_search_preferences SET {assignments} WHERE profile_key = 'primary';", list(changes.values()))
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps({"changed": changes, "applied": bool(args.apply)}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
