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


def regex_list(value: str | None) -> list[str] | None:
    """Parse regex preferences without treating regex commas as separators.

    A single CLI value is one regex. Multiple regexes can be supplied as a JSON
    array, which is unambiguous even when a pattern itself contains commas.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("location allow patterns JSON must be a valid array") from exc
        if not isinstance(parsed, list):
            raise ValueError("location allow patterns JSON must be an array")
        if any(not isinstance(item, str) for item in parsed):
            raise ValueError("location allow patterns JSON entries must be strings")
        return [item.strip() for item in parsed if item.strip()]
    # Preserve the historical comma-separated convenience for plain literal
    # locations, but never split a value that visibly uses regex syntax.  The
    # latter is what makes patterns such as ``^New York, NY$`` safe.  JSON is
    # the unambiguous representation for multiple complex patterns.
    if "," in text and not any(ch in text for ch in r"\^$*+?{}[]|()"):
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS typed discovery preferences")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("show")
    update = subs.add_parser("set")
    for field in FIELDS[:6]:
        help_text = None
        if field == "location_allow_patterns":
            help_text = "One regex exactly as written, or a JSON array of regexes; commas inside regexes are preserved."
        update.add_argument("--" + field.replace("_", "-"), help=help_text)
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
            raw = getattr(args, field)
            value = regex_list(raw) if field == "location_allow_patterns" else csv_list(raw)
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
