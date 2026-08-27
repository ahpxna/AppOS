#!/usr/bin/env python3
"""Manage the candidate-confirmed immigration profile and employer evidence.

This is intentionally a small, explicit CLI rather than a browser autofill
source.  It stores what the candidate confirms, records provenance for
employer evidence, and never derives a legal answer from an employer question.

Examples:
  python services/discovery/immigration_profile_v1.py show
  python services/discovery/immigration_profile_v1.py set \
    --current-status F1 --current-work-authorization yes \
    --requires-sponsorship-to-start no --requires-future-sponsorship yes --confirm --apply
  python services/discovery/immigration_profile_v1.py employer-evidence \
    --application-id <uuid> --kind everify --status verified \
    --source-url https://... --note "Exact employer name matched" --apply

The candidate is responsible for confirming dates, eligibility, and every
employer-form answer. This tool is not legal advice.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

import psycopg
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.common.config import database_dsn
from services.common.immigration_semantics import (
    EXACT_CANDIDATE_ADDITIONAL_CLASSES,
    ImmigrationQuestionClass,
)
from services.discovery.immigration_intelligence import (
    record_employer_evidence,
    record_jd_immigration_assessment,
)

# These are candidate attestations. Employer E-Verify is deliberately absent:
# it is employer evidence, not a candidate-provided legal profile field.
EXACT_ADDITIONAL_QUESTION_CLASSES = EXACT_CANDIDATE_ADDITIONAL_CLASSES


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD.") from exc


def cmd_show(cur) -> int:
    cur.execute(
        """
        SELECT profile_key, current_status, current_work_authorization,
               opt_eligible, opt_start_date, opt_end_date,
               stem_extension_eligible, stem_cip_code,
               requires_sponsorship_to_start, requires_future_sponsorship,
               us_citizen, us_person, permanent_work_authorization,
               user_confirmed_at, confirmation_note, confirmation_version
        FROM immigration_profiles WHERE profile_key = 'primary';
        """
    )
    row = cur.fetchone()
    print(json.dumps({
        "profile": None if not row else {
            "profile_key": row[0], "current_status": row[1],
            "current_work_authorization": row[2], "opt_eligible": row[3],
            "opt_start_date": str(row[4] or ""), "opt_end_date": str(row[5] or ""),
            "stem_extension_eligible": row[6], "stem_cip_code": row[7],
            "requires_sponsorship_to_start": row[8],
            "requires_future_sponsorship": row[9],
            "us_citizen": row[10], "us_person": row[11],
            "permanent_work_authorization": row[12],
            "user_confirmed_at": str(row[13] or ""), "confirmation_note": row[14],
            "confirmation_version": row[15],
        },
        "warning": "This profile does not answer employer questions automatically.",
    }, indent=2))
    return 0


def cmd_set(conn, args) -> int:
    if not args.confirm:
        print("REFUSED: add --confirm only after you personally verified the values.")
        return 1
    try:
        opt_start, opt_end = parse_date(args.opt_start_date), parse_date(args.opt_end_date)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    if opt_start and opt_end and opt_start > opt_end:
        print("ERROR: OPT start date must not be after OPT end date.")
        return 1
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_status, current_work_authorization, opt_eligible, opt_start_date,
                   opt_end_date, stem_extension_eligible, stem_cip_code,
                   requires_sponsorship_to_start, requires_future_sponsorship,
                   us_citizen, us_person, permanent_work_authorization, confirmation_note,
                   confirmation_version
            FROM immigration_profiles WHERE profile_key = 'primary';
            """
        )
        prior = cur.fetchone()
    prior_values = dict(zip((
        "current_status", "current_work_authorization", "opt_eligible", "opt_start_date",
        "opt_end_date", "stem_extension_eligible", "stem_cip_code",
        "requires_sponsorship_to_start", "requires_future_sponsorship",
        "us_citizen", "us_person", "permanent_work_authorization", "confirmation_note",
        "confirmation_version",
    ), prior or ()))
    defaults = {
        "current_status": "unconfirmed", "current_work_authorization": "unconfirmed",
        "requires_sponsorship_to_start": "unconfirmed", "requires_future_sponsorship": "unconfirmed",
        "us_citizen": "unconfirmed", "us_person": "unconfirmed",
        "permanent_work_authorization": "unconfirmed",
    }
    requested = {
        "current_status": args.current_status.strip() if args.current_status else None,
        "current_work_authorization": args.current_work_authorization,
        "opt_eligible": args.opt_eligible,
        "opt_start_date": opt_start,
        "opt_end_date": opt_end,
        "stem_extension_eligible": args.stem_extension_eligible,
        "stem_cip_code": (args.stem_cip_code or "").strip() or None,
        "requires_sponsorship_to_start": args.requires_sponsorship_to_start,
        "requires_future_sponsorship": args.requires_future_sponsorship,
        "us_citizen": args.us_citizen,
        "us_person": args.us_person,
        "permanent_work_authorization": args.permanent_work_authorization,
        "confirmation_note": (args.confirmation_note or "").strip() or None,
        "confirmation_version": int(prior_values.get("confirmation_version") or 0) + 1,
    }
    # argparse defaults are None for every mutable field. A profile update is
    # a patch: unspecified values retain the candidate-confirmed prior value.
    values = {
        key: (value if value is not None else prior_values.get(key, defaults.get(key)))
        for key, value in requested.items()
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO immigration_profiles
              (profile_key, current_status, current_work_authorization, opt_eligible,
               opt_start_date, opt_end_date, stem_extension_eligible, stem_cip_code,
               requires_sponsorship_to_start, requires_future_sponsorship,
               us_citizen, us_person, permanent_work_authorization,
               user_confirmed_at, confirmation_note, confirmation_version)
            VALUES ('primary', %(current_status)s, %(current_work_authorization)s, %(opt_eligible)s,
                    %(opt_start_date)s, %(opt_end_date)s, %(stem_extension_eligible)s, %(stem_cip_code)s,
                    %(requires_sponsorship_to_start)s, %(requires_future_sponsorship)s,
                    %(us_citizen)s, %(us_person)s, %(permanent_work_authorization)s,
                    now(), %(confirmation_note)s, %(confirmation_version)s)
            ON CONFLICT (profile_key) DO UPDATE
            SET current_status = EXCLUDED.current_status,
                current_work_authorization = EXCLUDED.current_work_authorization,
                opt_eligible = EXCLUDED.opt_eligible,
                opt_start_date = EXCLUDED.opt_start_date,
                opt_end_date = EXCLUDED.opt_end_date,
                stem_extension_eligible = EXCLUDED.stem_extension_eligible,
                stem_cip_code = EXCLUDED.stem_cip_code,
                requires_sponsorship_to_start = EXCLUDED.requires_sponsorship_to_start,
                requires_future_sponsorship = EXCLUDED.requires_future_sponsorship,
                us_citizen = EXCLUDED.us_citizen, us_person = EXCLUDED.us_person,
                permanent_work_authorization = EXCLUDED.permanent_work_authorization,
                user_confirmed_at = now(), confirmation_note = EXCLUDED.confirmation_note,
                confirmation_version = EXCLUDED.confirmation_version;
            """,
            values,
        )
        # Re-evaluate only the deterministic candidate-fit label for captured
        # JDs. This is safe to do in bulk because it reads the existing JD and
        # candidate-confirmed profile; it neither calls an LLM nor changes an
        # employer evidence record.
        cur.execute("SELECT id::text, jd_text FROM applications WHERE jd_text IS NOT NULL;")
        reassessed = 0
        for application_id, jd_text in cur.fetchall():
            record_jd_immigration_assessment(cur, application_id, str(jd_text or ""))
            reassessed += 1
    if args.apply:
        conn.commit()
        print(f"Saved candidate-confirmed immigration profile and re-assessed {reassessed} captured JD(s).")
        print("It is still review-only for employer forms.")
    else:
        conn.rollback()
        print("DRY RUN: profile not saved.")
    return 0


def cmd_employer_evidence(conn, args) -> int:
    if not args.source_url.startswith(("https://", "http://")):
        print("ERROR: --source-url must be an http(s) URL.")
        return 1
    allowed = {"everify": {"verified", "not_found", "unknown"},
               "h1b_history": {"positive", "none_found", "unknown"}}
    if args.status not in allowed[args.kind]:
        print(f"ERROR: invalid --status for {args.kind}.")
        return 1
    if not 0 <= args.confidence <= 1:
        print("ERROR: --confidence must be between 0 and 1.")
        return 1
    with conn.cursor() as cur:
        try:
            reassessed = record_employer_evidence(
                cur, application_id=args.application_id, kind=args.kind, status=args.status,
                source_url=args.source_url, source_name=args.source_name,
                note=args.note or "", confidence=args.confidence,
                legal_entity_name=(args.legal_entity_name or "").strip() or None,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            conn.rollback()
            return 1
    if args.apply:
        conn.commit()
        print(f"Employer evidence saved and {reassessed} matching JD(s) re-assessed. It is not a sponsorship guarantee.")
    else:
        conn.rollback()
        print("DRY RUN: employer evidence not saved.")
    return 0


def cmd_set_exact_answer(conn, args) -> int:
    """Store one candidate-confirmed answer for one precise question class."""
    question_class = ImmigrationQuestionClass(args.question_class)
    if question_class not in EXACT_ADDITIONAL_QUESTION_CLASSES:
        print("ERROR: this question class is not a candidate-only exact-answer field.")
        return 1
    if not args.confirm:
        print("REFUSED: add --confirm only after you personally verified this exact meaning.")
        return 1
    field_name = f"immigration:{question_class.value}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sensitive_answers
              (field_name, answer, answer_kind, requires_review, approved_by_user, question_hints, notes)
            VALUES (%s, %s, 'eligibility', false, true, '[]'::jsonb, %s)
            ON CONFLICT (field_name) DO UPDATE
            SET answer = EXCLUDED.answer, answer_kind = EXCLUDED.answer_kind,
                requires_review = false, approved_by_user = true,
                notes = EXCLUDED.notes, updated_at = now();
            """,
            (field_name, args.answer.title(),
             (args.note or "Candidate-confirmed exact immigration question class.").strip()),
        )
    if args.apply:
        conn.commit()
        print(f"Saved candidate-confirmed answer for {question_class.value}.")
    else:
        conn.rollback()
        print("DRY RUN: exact immigration answer not saved.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="JobOS immigration profile and employer-evidence ledger.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("--current-status")
    for name in (
        "current-work-authorization", "requires-sponsorship-to-start", "requires-future-sponsorship",
        "us-citizen", "us-person", "permanent-work-authorization",
    ):
        set_parser.add_argument(f"--{name}", choices=("yes", "no", "unconfirmed"), default=None)
    set_parser.add_argument("--opt-eligible", action=argparse.BooleanOptionalAction, default=None)
    set_parser.add_argument("--opt-start-date")
    set_parser.add_argument("--opt-end-date")
    set_parser.add_argument("--stem-extension-eligible", action=argparse.BooleanOptionalAction, default=None)
    set_parser.add_argument("--stem-cip-code")
    set_parser.add_argument("--confirmation-note")
    set_parser.add_argument("--confirm", action="store_true")
    set_parser.add_argument("--apply", action="store_true")
    evidence = sub.add_parser("employer-evidence")
    evidence.add_argument("--application-id", required=True)
    evidence.add_argument("--kind", choices=("everify", "h1b_history"), required=True)
    evidence.add_argument("--status", required=True)
    evidence.add_argument("--source-url", required=True)
    evidence.add_argument("--source-name", default="user supplied source")
    evidence.add_argument("--note")
    evidence.add_argument("--legal-entity-name")
    evidence.add_argument("--confidence", type=float, default=0.7)
    evidence.add_argument("--apply", action="store_true")
    exact = sub.add_parser("set-exact-answer", help="Save one candidate-confirmed STEM/I-983 answer.")
    exact.add_argument("--question-class", choices=[item.value for item in sorted(EXACT_ADDITIONAL_QUESTION_CLASSES, key=str)], required=True)
    exact.add_argument("--answer", choices=("yes", "no"), required=True)
    exact.add_argument("--note")
    exact.add_argument("--confirm", action="store_true")
    exact.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(database_dsn(), autocommit=False) as conn:
        if args.command == "show":
            with conn.cursor() as cur:
                return cmd_show(cur)
        if args.command == "set":
            return cmd_set(conn, args)
        if args.command == "employer-evidence":
            return cmd_employer_evidence(conn, args)
        return cmd_set_exact_answer(conn, args)


if __name__ == "__main__":
    raise SystemExit(main())
