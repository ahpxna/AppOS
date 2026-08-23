#!/usr/bin/env python3
"""Manage answers learned only after an explicit local human confirmation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn
from services.common.question_memory import normalize_question
from services.common.immigration_semantics import legal_question_pause_reason


def record(cur, args) -> dict[str, object]:
    normalized = normalize_question(args.question)
    if not normalized:
        raise ValueError("Question is required.")
    if legal_question_pause_reason(args.question):
        raise ValueError("Legal/immigration questions belong to the exact sensitive-answer workflow, not question memory.")
    if args.scope == "ats" and not args.ats_type:
        raise ValueError("--ats-type is required for ATS-scoped memory.")
    if args.scope == "company" and not args.company:
        raise ValueError("--company is required for company-scoped memory.")
    company = normalize_question(args.company) if args.company else None
    cur.execute(
        """INSERT INTO application_question_memory
              (scope, ats_type, company_normalized, question_normalized, answer_text,
               answer_kind, confidence, user_confirmed_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, 1.0, now(), now())
           ON CONFLICT (scope, ats_type, company_normalized, question_normalized)
           DO UPDATE SET answer_text = EXCLUDED.answer_text, answer_kind = EXCLUDED.answer_kind,
                         confidence = 1.0, user_confirmed_at = now(), updated_at = now()
           RETURNING id::text;""",
        (args.scope, args.ats_type, company, normalized, args.answer, args.answer_kind),
    )
    return {"id": cur.fetchone()[0], "scope": args.scope, "question": normalized, "confirmed": True}


def list_memory(cur) -> list[dict[str, object]]:
    cur.execute(
        """SELECT id::text, scope, ats_type, company_normalized, question_normalized,
                  answer_kind, confidence, use_count, last_used_at, user_confirmed_at
             FROM application_question_memory ORDER BY updated_at DESC;"""
    )
    return [dict(zip(("id", "scope", "ats_type", "company", "question", "answer_kind",
                      "confidence", "use_count", "last_used_at", "confirmed_at"), row)) for row in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS human-confirmed application question memory")
    subs = parser.add_subparsers(dest="command", required=True)
    add = subs.add_parser("confirm", help="Store one reviewed non-legal answer.")
    add.add_argument("--scope", choices=("global", "ats", "company"), default="global")
    add.add_argument("--ats-type")
    add.add_argument("--company")
    add.add_argument("--question", required=True)
    add.add_argument("--answer", required=True)
    add.add_argument("--answer-kind", choices=("text", "option"), default="text")
    add.add_argument("--apply", action="store_true")
    subs.add_parser("list", help="List memory metadata; answers are not printed.")
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        if args.command == "list":
            print(json.dumps({"items": list_memory(cur)}, default=str, indent=2))
            return 0
        result = record(cur, args)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
            result["dry_run"] = True
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
