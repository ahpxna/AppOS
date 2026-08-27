"""Atomic budget reservation and per-call LLM cost accounting.

Paid API calls fail closed when pricing/budget state cannot be established.
Local-model accounting is best-effort and always records zero marginal USD when
PostgreSQL is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
import os
import uuid
from typing import Any


class LLMBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    id: str
    reserved_cost_usd: Decimal
    model_name: str
    provider: str
    # Kept optional for in-process callers/tests created before immutable
    # budget-date ownership. Settlement always reloads the authoritative date
    # from the reservation row under lock.
    budget_date: date | None = None
    llm_call_id: str | None = None


def _application_id() -> str | None:
    raw = (os.getenv("JOBOS_APPLICATION_ID") or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


def _workflow_step_run_id() -> str | None:
    raw = (os.getenv("JOBOS_WORKFLOW_STEP_RUN_ID") or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


def _price(cur, model: str, *, require_priced: bool) -> tuple[Decimal, Decimal, bool]:
    cur.execute(
        """SELECT input_usd_per_1k, output_usd_per_1k, is_local, notes
             FROM model_pricing WHERE model_name=%s;""", (model,)
    )
    row = cur.fetchone()
    if not row:
        if require_priced:
            raise LLMBudgetError(f"Paid LLM model {model!r} has no model_pricing row; set a real price before API use.")
        return Decimal(0), Decimal(0), True
    inp, out, is_local, notes = row
    unpriced = (not is_local and str(notes or "").upper().startswith("PRICING NOT SET"))
    if require_priced and unpriced:
        raise LLMBudgetError(f"Paid LLM model {model!r} is marked PRICING NOT SET; hard USD budget cannot authorize it.")
    return Decimal(inp or 0), Decimal(out or 0), bool(is_local)


def reserve_paid_call(*, role: str, provider: str, model: str,
                      estimated_input_tokens: int, max_output_tokens: int,
                      request_sha256: str | None = None, request_kind: str = 'chat') -> Reservation:
    try:
        import psycopg
        from services.common.config import database_dsn
    except Exception as exc:
        raise LLMBudgetError(f"Cannot load PostgreSQL accounting for paid LLM call: {exc}") from exc
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT CURRENT_DATE;")
        budget_date = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO daily_budgets(date,max_cost_usd,max_jobs_full_pipeline,max_browser_tasks)
               VALUES (%s,2.00,20,50) ON CONFLICT (date) DO NOTHING;""",
            (budget_date,),
        )
        cur.execute(
            """SELECT max_cost_usd,current_cost_usd FROM daily_budgets
                 WHERE date=%s FOR UPDATE;""",
            (budget_date,),
        )
        max_usd, current = cur.fetchone()
        in_price, out_price, is_local = _price(cur, model, require_priced=True)
        if is_local:
            raise LLMBudgetError(f"API backend model {model!r} is marked local in model_pricing; fix pricing metadata.")
        reserve = (Decimal(max(0, estimated_input_tokens)) / Decimal(1000) * in_price
                   + Decimal(max(0, max_output_tokens)) / Decimal(1000) * out_price)
        cur.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) FROM cost_ledger WHERE budget_date=%s;", (budget_date,))
        ledger_spent = Decimal(cur.fetchone()[0] or 0)
        authoritative = max(Decimal(current or 0), ledger_spent)
        if max_usd is not None and authoritative + reserve > Decimal(max_usd):
            raise LLMBudgetError(
                f"Paid LLM call blocked: reserving ${reserve:.4f} would exceed daily budget "
                f"${Decimal(max_usd):.2f} (already reserved/settled ${authoritative:.4f})."
            )
        llm_call_id = None
        if request_sha256:
            cur.execute(
                """INSERT INTO llm_calls(
                       workflow_step_run_id,application_id,role,provider,configured_model,resolved_model,
                       request_kind,request_sha256,status,started_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running',now()) RETURNING id::text;""",
                (_workflow_step_run_id(), _application_id(), role, provider, model, model, request_kind, request_sha256),
            )
            llm_call_id = str(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO llm_call_attempts(llm_call_id,attempt_no,status)
                   VALUES (%s,1,'started');""", (llm_call_id,),
            )
        cur.execute(
            """INSERT INTO llm_cost_reservations(
                   application_id,role,provider,model_name,reserved_cost_usd,budget_date,llm_call_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id::text;""",
            (_application_id(), role, provider, model, reserve, budget_date, llm_call_id),
        )
        rid = str(cur.fetchone()[0])
        if llm_call_id:
            cur.execute("UPDATE llm_calls SET reservation_id=%s WHERE id=%s;", (rid, llm_call_id))
        cur.execute(
            "UPDATE daily_budgets SET current_cost_usd=%s WHERE date=%s;",
            (authoritative + reserve, budget_date),
        )
        conn.commit()
    return Reservation(rid, reserve, model, provider, budget_date, llm_call_id)


def _record_ledger(cur, *, role: str, provider: str, configured_model: str,
                   resolved_model: str, input_tokens: int, output_tokens: int,
                   cost: Decimal, request_id: str | None, is_local: bool,
                   budget_date: date | None = None, llm_call_id: str | None = None) -> None:
    cur.execute(
        """INSERT INTO cost_ledger(application_id,agent_name,model_name,input_tokens,output_tokens,
                    estimated_cost_usd,task_type,is_local,provider,provider_request_id,resolved_model_name,budget_date,llm_call_id)
           VALUES (%s,%s,%s,%s,%s,%s,'single_call',%s,%s,%s,%s,%s,%s);""",
        (_application_id(), role, configured_model, int(input_tokens), int(output_tokens), cost,
         is_local, provider, request_id, resolved_model, budget_date, llm_call_id),
    )


def settle_paid_call(reservation: Reservation, *, role: str, configured_model: str,
                     resolved_model: str, input_tokens: int, output_tokens: int,
                     request_id: str | None, response_sha256: str | None = None) -> Decimal:
    import psycopg
    from services.common.config import database_dsn
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reserved_cost_usd,status,budget_date FROM llm_cost_reservations WHERE id=%s FOR UPDATE;",
            (reservation.id,),
        )
        row = cur.fetchone()
        if not row or row[1] != "reserved":
            raise LLMBudgetError("LLM cost reservation is missing or no longer open.")
        reserved, budget_date = Decimal(row[0] or 0), row[2]
        try:
            in_price, out_price, _ = _price(cur, resolved_model, require_priced=True)
            actual = (Decimal(max(0, input_tokens)) / Decimal(1000) * in_price
                      + Decimal(max(0, output_tokens)) / Decimal(1000) * out_price)
            pricing_note = "resolved_model_priced"
        except LLMBudgetError:
            # Never under-report a dynamically routed/unpriced paid model. Keep
            # the full conservative reservation as its accounted cost.
            actual = reserved
            pricing_note = "resolved_model_unpriced_kept_reserved_cost"
        _record_ledger(cur, role=role, provider=reservation.provider,
                       configured_model=configured_model, resolved_model=resolved_model,
                       input_tokens=input_tokens, output_tokens=output_tokens,
                       cost=actual, request_id=request_id, is_local=False, budget_date=budget_date,
                       llm_call_id=reservation.llm_call_id)
        cur.execute(
            """UPDATE llm_cost_reservations SET status='settled',settled_at=now(),detail_json=%s
                 WHERE id=%s;""",
            (json.dumps({"actual_cost_usd": str(actual), "pricing": pricing_note}), reservation.id),
        )
        if reservation.llm_call_id:
            cur.execute(
                """UPDATE llm_calls SET status='completed',resolved_model=%s,provider_request_id=%s,
                          input_tokens=%s,output_tokens=%s,response_sha256=%s,finished_at=now() WHERE id=%s;""",
                (resolved_model, request_id, int(input_tokens), int(output_tokens), response_sha256, reservation.llm_call_id),
            )
            cur.execute(
                """UPDATE llm_call_attempts SET status='completed',provider_request_id=%s,finished_at=now()
                    WHERE llm_call_id=%s AND attempt_no=1;""", (request_id, reservation.llm_call_id),
            )
        cur.execute("SELECT current_cost_usd FROM daily_budgets WHERE date=%s FOR UPDATE;", (budget_date,))
        current = Decimal((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            "UPDATE daily_budgets SET current_cost_usd=%s WHERE date=%s;",
            (max(Decimal(0), current - reserved + actual), budget_date),
        )
        conn.commit()
        return actual


def mark_paid_call_uncertain(reservation: Reservation, *, role: str, configured_model: str,
                             estimated_input_tokens: int, error: str) -> None:
    """Keep the reservation charged when network outcome may be uncertain."""
    try:
        import psycopg
        from services.common.config import database_dsn
        with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE llm_cost_reservations SET status='uncertain',settled_at=now(),detail_json=%s
                     WHERE id=%s AND status='reserved'
                 RETURNING budget_date;""",
                (json.dumps({"error": error[:500]}), reservation.id),
            )
            row = cur.fetchone()
            if row:
                _record_ledger(cur, role=role, provider=reservation.provider,
                               configured_model=configured_model, resolved_model=configured_model,
                               input_tokens=estimated_input_tokens, output_tokens=0,
                               cost=reservation.reserved_cost_usd, request_id=None, is_local=False,
                               budget_date=row[0], llm_call_id=reservation.llm_call_id)
                if reservation.llm_call_id:
                    cur.execute(
                        """UPDATE llm_calls SET status='uncertain',error_message=%s,finished_at=now() WHERE id=%s;""",
                        (error[:1000], reservation.llm_call_id),
                    )
                    cur.execute(
                        """UPDATE llm_call_attempts SET status='uncertain',error_message=%s,finished_at=now()
                            WHERE llm_call_id=%s AND attempt_no=1;""",
                        (error[:1000], reservation.llm_call_id),
                    )
            conn.commit()
    except Exception:
        # The DB reservation was already durably charged before the call. Do not
        # hide the original transport exception if settlement bookkeeping fails.
        return


def record_local_call(*, role: str, provider: str, model: str,
                      input_tokens: int, output_tokens: int, request_id: str | None = None,
                      request_sha256: str | None = None, response_sha256: str | None = None,
                      request_kind: str = 'chat') -> None:
    try:
        import psycopg
        from services.common.config import database_dsn
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            llm_call_id = None
            if request_sha256:
                cur.execute(
                    """INSERT INTO llm_calls(workflow_step_run_id,application_id,role,provider,configured_model,resolved_model,
                               request_kind,request_sha256,status,provider_request_id,input_tokens,output_tokens,response_sha256,
                               started_at,finished_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,now(),now()) RETURNING id::text;""",
                    (_workflow_step_run_id(),_application_id(),role,provider,model,model,request_kind,request_sha256,request_id,
                     int(input_tokens),int(output_tokens),response_sha256),
                )
                llm_call_id = str(cur.fetchone()[0])
                cur.execute(
                    """INSERT INTO llm_call_attempts(llm_call_id,attempt_no,status,provider_request_id,started_at,finished_at)
                       VALUES (%s,1,'completed',%s,now(),now());""", (llm_call_id,request_id),
                )
            _record_ledger(cur, role=role, provider=provider, configured_model=model,
                           resolved_model=model, input_tokens=input_tokens,
                           output_tokens=output_tokens, cost=Decimal(0), request_id=request_id,
                           is_local=True, llm_call_id=llm_call_id)
    except Exception:
        # Local calls have zero marginal USD. Lack of observability must not turn
        # an otherwise offline/local inference into a production hard failure.
        return
