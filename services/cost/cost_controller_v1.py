"""
L1 -- COST CONTROLLER

Records model spend into cost_ledger and answers one question for the
orchestrator: may this step run right now?

Design notes:
  Local models are recorded at zero. That is not a fudge; it is the reason the
  per-claim truth checker is affordable at all. A resume with eight bullets
  costs eight verifier calls, which would dominate the bill on a paid model
  and cost nothing on Ollama. Making that visible in the ledger is the point.

  Paid models are seeded at zero price in model_pricing with a NOT SET note.
  An unpriced model reports zero spend and is flagged, rather than silently
  guessing a number that would under-report the bill.

  Since migration 076, every gateway LLM call writes cost_ledger directly and
  paid calls reserve the hard daily budget before network I/O. ``backfill`` is
  retained only for legacy component_runs created before that migration, so it
  cannot double-charge directly-accounted calls.

Usage:
  python services/cost/cost_controller_v1.py backfill --apply
  python services/cost/cost_controller_v1.py report
  python services/cost/cost_controller_v1.py check --task full_pipeline
  python services/cost/cost_controller_v1.py set-budget --max-usd 2.00
  python services/cost/cost_controller_v1.py price --model X --input 0.15 --output 0.60
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn
from services.control_plane.budget_admissions import admit

DSN = database_dsn()

CONTROLLER_VERSION = "cost_controller_v1_2026_07_29"

TASK_KINDS = ("full_pipeline", "browser_task", "single_call")


def ensure_today_budget(cur) -> None:
    cur.execute(
        """
        INSERT INTO daily_budgets
          (date, max_cost_usd, max_jobs_full_pipeline, max_browser_tasks)
        VALUES (CURRENT_DATE, 2.00, 20, 50)
        ON CONFLICT (date) DO NOTHING;
        """
    )
    cur.execute(
        """UPDATE daily_budgets d
              SET current_cost_usd = GREATEST(
                    COALESCE(d.current_cost_usd,0),
                    COALESCE((SELECT SUM(estimated_cost_usd) FROM cost_ledger
                               WHERE COALESCE(budget_date, created_at::date)=CURRENT_DATE),0))
            WHERE d.date=CURRENT_DATE;"""
    )


def price_for(cur, model_name: Optional[str]) -> Tuple[Decimal, Decimal, bool, bool]:
    """Returns (input_per_1k, output_per_1k, is_local, is_priced)."""
    name = model_name or "unknown"
    cur.execute(
        """
        SELECT input_usd_per_1k, output_usd_per_1k, is_local, notes
        FROM model_pricing WHERE model_name = %s;
        """,
        (name,),
    )
    row = cur.fetchone()
    if not row:
        # Unknown model: record it so it shows up as unpriced rather than
        # vanishing from the ledger.
        cur.execute(
            """
            INSERT INTO model_pricing (model_name, provider, is_local, notes)
            VALUES (%s, 'unknown', false, 'Auto-added. PRICING NOT SET.')
            ON CONFLICT (model_name) DO NOTHING;
            """,
            (name,),
        )
        return Decimal(0), Decimal(0), False, False

    in_p, out_p, is_local, notes = row
    is_priced = is_local or not (notes or "").upper().startswith("PRICING NOT SET")
    return Decimal(in_p or 0), Decimal(out_p or 0), bool(is_local), is_priced


def cmd_backfill(conn, args) -> int:
    with conn.cursor() as cur:
        ensure_today_budget(cur)

        cur.execute(
            """
            SELECT cr.id::text, cr.application_id::text, cr.component_name,
                   cr.model_name, cr.input_tokens, cr.output_tokens, cr.task_type
            FROM component_runs cr
            LEFT JOIN cost_ledger cl ON cl.component_run_id = cr.id
            WHERE cl.id IS NULL
              AND cr.created_at < COALESCE(
                    (SELECT applied_at FROM schema_migrations
                      WHERE migration_id='076_workflow_integrity_orchestrator_leases_and_callback_binding.sql'),
                    'infinity'::timestamptz)
            ORDER BY cr.created_at;
            """
        )
        runs = cur.fetchall()
        if not runs:
            print("Nothing to backfill; every component run is already billed.")
            return 0

        total = Decimal(0)
        unpriced = 0
        for (run_id, app_id, component, model,
             in_tok, out_tok, task_type) in runs:
            in_p, out_p, is_local, is_priced = price_for(cur, model)
            if not is_priced:
                unpriced += 1

            cost = (Decimal(in_tok or 0) / 1000 * in_p
                    + Decimal(out_tok or 0) / 1000 * out_p)
            total += cost

            cur.execute(
                """
                INSERT INTO cost_ledger
                  (application_id, agent_name, model_name,
                   input_tokens, output_tokens, estimated_cost_usd,
                   component_run_id, task_type, is_local, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (component_run_id)
                WHERE component_run_id IS NOT NULL DO NOTHING;
                """,
                (app_id, component, model, in_tok or 0, out_tok or 0,
                 cost, run_id, task_type, is_local),
            )

        print(f"  billed {len(runs)} component run(s)")
        print(f"  total: ${total:.4f}")
        if unpriced:
            print(f"  WARNING: {unpriced} run(s) used a model with no price set.")
            print("  Their cost is recorded as $0. Set real prices with:")
            print("    cost_controller_v1.py price --model NAME --input X --output Y")

        if not args.apply:
            conn.rollback()
            print("\nDRY RUN. Nothing committed.")
            return 0
        conn.commit()
        print("\nCommitted.")
    return 0


def cmd_report(conn, args) -> int:
    with conn.cursor() as cur:
        ensure_today_budget(cur)
        conn.commit()

        cur.execute("SELECT spent_usd, calls, local_calls, paid_calls, "
                    "input_tokens, output_tokens FROM v_cost_today;")
        spent, calls, local_calls, paid_calls, in_tok, out_tok = cur.fetchone()

        cur.execute(
            """
            SELECT max_cost_usd, max_jobs_full_pipeline, max_browser_tasks,
                   current_jobs_full_pipeline, current_browser_tasks
            FROM daily_budgets WHERE date = CURRENT_DATE;
            """
        )
        max_usd, max_jobs, max_tasks, cur_jobs, cur_tasks = cur.fetchone()
        cur.execute("SELECT current_cost_usd FROM daily_budgets WHERE date=CURRENT_DATE;")
        reserved_or_settled = Decimal(cur.fetchone()[0] or 0)

        print(f"\n--- TODAY ---")
        print(f"  ledger spend:  ${spent:.4f} / ${max_usd:.2f}")
        print(f"  hard budget:   ${reserved_or_settled:.4f} reserved/settled")
        print(f"  calls:         {calls}  (local {local_calls}, paid {paid_calls})")
        print(f"  tokens:        {in_tok} in, {out_tok} out")
        print(f"  full pipeline: {cur_jobs} / {max_jobs}")
        print(f"  browser tasks: {cur_tasks} / {max_tasks}")

        cur.execute(
            """
            SELECT agent_name, model_name, is_local, calls, total_usd
            FROM v_cost_by_component LIMIT 15;
            """
        )
        rows = cur.fetchall()
        if rows:
            print(f"\n--- BY COMPONENT (all time) ---")
            print(f"  {'COMPONENT':<26} {'MODEL':<22} {'CALLS':>6} {'USD':>10}")
            for agent, model, is_local, ncalls, usd in rows:
                tag = "*" if is_local else " "
                print(f"{tag} {(agent or '?'):<26} {(model or '?'):<22} "
                      f"{ncalls:>6} {usd:>10.4f}")
            print("  * = local model, no marginal cost")

        cur.execute(
            """
            SELECT company, job_title, calls, total_usd
            FROM v_cost_by_application WHERE calls > 0 LIMIT 10;
            """
        )
        rows = cur.fetchall()
        if rows:
            print(f"\n--- BY APPLICATION ---")
            for company, title, ncalls, usd in rows:
                print(f"  {(company or '?'):<22} {(title or '?'):<28} "
                      f"{ncalls:>4} calls  ${usd:.4f}")

        cur.execute(
            """
            SELECT model_name, provider FROM model_pricing
            WHERE NOT is_local AND notes LIKE 'PRICING NOT SET%';
            """
        )
        unpriced = cur.fetchall()
        if unpriced:
            print(f"\n--- UNPRICED MODELS ---")
            for name, provider in unpriced:
                print(f"  {name} ({provider})")
            print("  Spend on these reports as $0 until you set a price.")
    return 0


def cmd_check(conn, args) -> int:
    """Returns exit 0 if the work may proceed, 1 if the budget refuses it."""
    with conn.cursor() as cur:
        # The budget row is the admission mutex.  Do not commit the setup
        # before this lock: two independently admitted applications must not
        # both observe the final remaining quota slot.
        ensure_today_budget(cur)
        cur.execute(
            """SELECT max_cost_usd, max_jobs_full_pipeline, max_browser_tasks,
                      current_jobs_full_pipeline, current_browser_tasks, current_cost_usd
                 FROM daily_budgets WHERE date = CURRENT_DATE FOR UPDATE;"""
        )
        budget_row = cur.fetchone()
        if not budget_row:
            raise RuntimeError("Today's budget row was not materialized.")

        cur.execute("SELECT spent_usd FROM v_cost_today;")
        ledger_spent = Decimal(cur.fetchone()[0] or 0)
        max_usd, max_jobs, max_tasks, cur_jobs, cur_tasks, current_cost = budget_row
        spent = max(ledger_spent, Decimal(current_cost or 0))

        already_admitted = False
        # A retry of an already-admitted subject must remain allowed even if
        # the daily count is now at its cap.  Paid-dollar admission remains
        # separately enforced by llm_cost_accounting_v1.
        if args.increment and args.subject_id and args.task in {"full_pipeline", "browser_task"}:
            cur.execute(
                """SELECT 1 FROM budget_admissions
                     WHERE budget_date=CURRENT_DATE AND task_kind=%s
                       AND subject_type=%s AND subject_id=%s;""",
                (args.task, args.subject_type, args.subject_id),
            )
            already_admitted = bool(cur.fetchone())

        reasons = []
        if max_usd is not None and spent >= Decimal(max_usd):
            reasons.append(f"daily spend ${spent:.4f} has reached ${max_usd:.2f}")
        if args.task == "full_pipeline" and max_jobs and cur_jobs >= max_jobs and (not args.subject_id or not already_admitted):
            reasons.append(f"full-pipeline runs {cur_jobs} has reached {max_jobs}")
        if args.task == "browser_task" and max_tasks and cur_tasks >= max_tasks and (not args.subject_id or not already_admitted):
            reasons.append(f"browser tasks {cur_tasks} has reached {max_tasks}")

        if reasons:
            conn.rollback()
            print("  BLOCKED: " + "; ".join(reasons))
            print("  Raise the limit with set-budget, or wait until tomorrow.")
            return 1

        newly_admitted = False
        if args.increment and args.subject_id and args.task in {"full_pipeline", "browser_task"}:
            admission = admit(
                cur, task_kind=args.task, subject_type=args.subject_type,
                subject_id=args.subject_id,
            )
            newly_admitted = admission.newly_admitted

        print(f"  OK: ${spent:.4f} spent of ${max_usd:.2f}; {args.task} allowed")

        if args.increment:
            col = {"full_pipeline": "current_jobs_full_pipeline",
                   "browser_task": "current_browser_tasks"}.get(args.task)
            if col and (not args.subject_id or newly_admitted):
                cur.execute(
                    f"UPDATE daily_budgets SET {col} = {col} + 1 WHERE date = CURRENT_DATE;"
                )
            conn.commit()
    return 0


def cmd_set_budget(conn, args) -> int:
    with conn.cursor() as cur:
        ensure_today_budget(cur)
        sets, vals = [], []
        if args.max_usd is not None:
            sets.append("max_cost_usd = %s"); vals.append(args.max_usd)
        if args.max_jobs is not None:
            sets.append("max_jobs_full_pipeline = %s"); vals.append(args.max_jobs)
        if args.max_browser_tasks is not None:
            sets.append("max_browser_tasks = %s"); vals.append(args.max_browser_tasks)
        if args.reset_counters:
            sets.append("current_jobs_full_pipeline = 0")
            sets.append("current_browser_tasks = 0")

        if not sets:
            print("Nothing to change.")
            return 0

        cur.execute(
            f"UPDATE daily_budgets SET {', '.join(sets)} WHERE date = CURRENT_DATE;",
            vals,
        )
        conn.commit()
        print("Budget updated for today.")
    return 0


def cmd_price(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_pricing
              (model_name, provider, input_usd_per_1k, output_usd_per_1k,
               is_local, notes, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (model_name) DO UPDATE
            SET provider = EXCLUDED.provider,
                input_usd_per_1k = EXCLUDED.input_usd_per_1k,
                output_usd_per_1k = EXCLUDED.output_usd_per_1k,
                is_local = EXCLUDED.is_local,
                notes = EXCLUDED.notes,
                updated_at = now();
            """,
            (args.model, args.provider, args.input, args.output,
             args.local, args.note or "Set manually."),
        )
        conn.commit()
        print(f"Priced {args.model}: ${args.input}/1k in, ${args.output}/1k out")
        print("Run 'backfill --apply' to reprice future runs (existing rows keep "
              "the price that was in effect when they were billed).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L1 cost controller")
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("backfill")
    pb.add_argument("--apply", action="store_true")

    sub.add_parser("report")

    pk = sub.add_parser("check")
    pk.add_argument("--task", default="single_call", choices=TASK_KINDS)
    pk.add_argument("--increment", action="store_true")
    pk.add_argument("--subject-type", default="application")
    pk.add_argument("--subject-id", help="Stable retry identity for quota admission.")

    ps = sub.add_parser("set-budget")
    ps.add_argument("--max-usd", type=float)
    ps.add_argument("--max-jobs", type=int)
    ps.add_argument("--max-browser-tasks", type=int)
    ps.add_argument("--reset-counters", action="store_true")

    pp = sub.add_parser("price")
    pp.add_argument("--model", required=True)
    pp.add_argument("--provider", default="openrouter")
    pp.add_argument("--input", type=float, required=True)
    pp.add_argument("--output", type=float, required=True)
    pp.add_argument("--local", action="store_true")
    pp.add_argument("--note")

    args = p.parse_args()
    print(f"===== COST CONTROLLER ({CONTROLLER_VERSION}) =====")

    with psycopg.connect(DSN, autocommit=False) as conn:
        return {
            "backfill": cmd_backfill,
            "report": cmd_report,
            "check": cmd_check,
            "set-budget": cmd_set_budget,
            "price": cmd_price,
        }[args.command](conn, args)


if __name__ == "__main__":
    sys.exit(main())
