"""Atomic budget reservation and per-call LLM cost accounting.

Paid API calls fail closed when pricing/budget state cannot be established.
Local-model accounting is best-effort and always records zero marginal USD when
PostgreSQL is available.
"""
from __future__ import annotations

from dataclasses import dataclass
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


def _application_id() -> str | None:
    raw = (os.getenv("JOBOS_APPLICATION_ID") or "").strip()
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
                      estimated_input_tokens: int, max_output_tokens: int) -> Reservation:
    try:
        import psycopg
        from services.common.config import database_dsn
    except Exception as exc:
        raise LLMBudgetError(f"Cannot load PostgreSQL accounting for paid LLM call: {exc}") from exc
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO daily_budgets(date,max_cost_usd,max_jobs_full_pipeline,max_browser_tasks)
               VALUES (CURRENT_DATE,2.00,20,50) ON CONFLICT (date) DO NOTHING;"""
        )
        cur.execute(
            """SELECT max_cost_usd,current_cost_usd FROM daily_budgets
                 WHERE date=CURRENT_DATE FOR UPDATE;"""
        )
        max_usd, current = cur.fetchone()
        in_price, out_price, is_local = _price(cur, model, require_priced=True)
        if is_local:
            raise LLMBudgetError(f"API backend model {model!r} is marked local in model_pricing; fix pricing metadata.")
        reserve = (Decimal(max(0, estimated_input_tokens)) / Decimal(1000) * in_price
                   + Decimal(max(0, max_output_tokens)) / Decimal(1000) * out_price)
        cur.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) FROM cost_ledger WHERE created_at::date=CURRENT_DATE;")
        ledger_spent = Decimal(cur.fetchone()[0] or 0)
        authoritative = max(Decimal(current or 0), ledger_spent)
        if max_usd is not None and authoritative + reserve > Decimal(max_usd):
            raise LLMBudgetError(
                f"Paid LLM call blocked: reserving ${reserve:.4f} would exceed daily budget "
                f"${Decimal(max_usd):.2f} (already reserved/settled ${authoritative:.4f})."
            )
        cur.execute(
            """INSERT INTO llm_cost_reservations(application_id,role,provider,model_name,reserved_cost_usd)
               VALUES (%s,%s,%s,%s,%s) RETURNING id::text;""",
            (_application_id(), role, provider, model, reserve),
        )
        rid = str(cur.fetchone()[0])
        cur.execute(
            "UPDATE daily_budgets SET current_cost_usd=%s WHERE date=CURRENT_DATE;",
            (authoritative + reserve,),
        )
        conn.commit()
    return Reservation(rid, reserve, model, provider)


def _record_ledger(cur, *, role: str, provider: str, configured_model: str,
                   resolved_model: str, input_tokens: int, output_tokens: int,
                   cost: Decimal, request_id: str | None, is_local: bool) -> None:
    cur.execute(
        """INSERT INTO cost_ledger(application_id,agent_name,model_name,input_tokens,output_tokens,
                    estimated_cost_usd,task_type,is_local,provider,provider_request_id,resolved_model_name)
           VALUES (%s,%s,%s,%s,%s,%s,'single_call',%s,%s,%s,%s);""",
        (_application_id(), role, configured_model, int(input_tokens), int(output_tokens), cost,
         is_local, provider, request_id, resolved_model),
    )


def settle_paid_call(reservation: Reservation, *, role: str, configured_model: str,
                     resolved_model: str, input_tokens: int, output_tokens: int,
                     request_id: str | None) -> Decimal:
    import psycopg
    from services.common.config import database_dsn
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reserved_cost_usd,status FROM llm_cost_reservations WHERE id=%s FOR UPDATE;",
            (reservation.id,),
        )
        row = cur.fetchone()
        if not row or row[1] != "reserved":
            raise LLMBudgetError("LLM cost reservation is missing or no longer open.")
        reserved = Decimal(row[0] or 0)
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
                       cost=actual, request_id=request_id, is_local=False)
        cur.execute(
            """UPDATE llm_cost_reservations SET status='settled',settled_at=now(),detail_json=%s
                 WHERE id=%s;""",
            (json.dumps({"actual_cost_usd": str(actual), "pricing": pricing_note}), reservation.id),
        )
        cur.execute("SELECT current_cost_usd FROM daily_budgets WHERE date=CURRENT_DATE FOR UPDATE;")
        current = Decimal((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            "UPDATE daily_budgets SET current_cost_usd=%s WHERE date=CURRENT_DATE;",
            (max(Decimal(0), current - reserved + actual),),
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
                     WHERE id=%s AND status='reserved';""",
                (json.dumps({"error": error[:500]}), reservation.id),
            )
            if cur.rowcount:
                _record_ledger(cur, role=role, provider=reservation.provider,
                               configured_model=configured_model, resolved_model=configured_model,
                               input_tokens=estimated_input_tokens, output_tokens=0,
                               cost=reservation.reserved_cost_usd, request_id=None, is_local=False)
            conn.commit()
    except Exception:
        # The DB reservation was already durably charged before the call. Do not
        # hide the original transport exception if settlement bookkeeping fails.
        return


def record_local_call(*, role: str, provider: str, model: str,
                      input_tokens: int, output_tokens: int, request_id: str | None = None) -> None:
    try:
        import psycopg
        from services.common.config import database_dsn
        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
            _record_ledger(cur, role=role, provider=provider, configured_model=model,
                           resolved_model=model, input_tokens=input_tokens,
                           output_tokens=output_tokens, cost=Decimal(0), request_id=request_id,
                           is_local=True)
    except Exception:
        # Local calls have zero marginal USD. Lack of observability must not turn
        # an otherwise offline/local inference into a production hard failure.
        return
