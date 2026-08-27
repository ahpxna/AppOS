"""The canonical transactional application-state mutation boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb


class PipelineStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransitionResult:
    application_id: str
    from_step: str
    to_step: str
    changed: bool


class PipelineStateStore:
    """Validate a declared edge, CAS state, then append its audit event.

    This method is deliberately side-effect free beyond the transaction: no
    browser command, LLM call, process or notification belongs here.
    """

    def transition(
        self,
        cur,
        *,
        application_id: str,
        expected_from: str,
        to: str,
        actor: str,
        reason: str,
        detail: Mapping[str, Any] | None = None,
        status: str | None = None,
        require_automated: bool | None = None,
        required_kind: str | None = None,
        lease_run_id: str | None = None,
        expected_job_url: str | None = None,
        expected_jd_hash: str | None = None,
        guard_sql: str | None = None,
        guard_params: Sequence[Any] = (),
        allow_already_target: bool = True,
    ) -> TransitionResult:
        cur.execute(
            "SELECT automated, coalesce(transition_kind, CASE WHEN automated THEN 'automated' ELSE 'human' END) "
            "FROM pipeline_transitions WHERE from_step=%s AND to_step=%s;",
            (expected_from, to),
        )
        edge = cur.fetchone()
        if not edge:
            raise PipelineStateError(f"Illegal transition {expected_from!r} -> {to!r}.")
        edge_kind = str(edge[1])
        if required_kind is not None:
            if required_kind not in {"automated", "human", "privileged", "recovery"}:
                raise ValueError(f"unknown required transition kind: {required_kind!r}")
            if edge_kind != required_kind:
                raise PipelineStateError(
                    f"Transition {expected_from!r} -> {to!r} is {edge_kind!r}, not {required_kind!r}."
                )
        elif require_automated is True and edge_kind != "automated":
            raise PipelineStateError(f"Transition {expected_from!r} -> {to!r} is not an automated edge.")
        elif require_automated is False and edge_kind != "human":
            raise PipelineStateError(f"Transition {expected_from!r} -> {to!r} is not a human-decision edge.")
        if guard_sql and ";" in guard_sql:
            raise ValueError("pipeline transition guard must be one SQL predicate, not a statement")
        sql = (
            "UPDATE applications SET current_step=%s, updated_at=now()"
            + (", status=%s" if status is not None else "")
            + " WHERE id=%s AND current_step=%s"
        )
        params: list[Any] = [to]
        if status is not None:
            params.append(status)
        params.extend([application_id, expected_from])
        if lease_run_id:
            sql += " AND processing_run_id=%s::uuid AND processing_step=%s AND processing_lease_expires_at>now()"
            params.extend([lease_run_id, expected_from])
        if expected_job_url is not None:
            sql += " AND coalesce(job_url,'')=%s"
            params.append(expected_job_url)
        if expected_jd_hash is not None:
            sql += " AND coalesce(jd_hash,'')=%s"
            params.append(expected_jd_hash)
        if guard_sql:
            sql += f" AND ({guard_sql})"
            params.extend(guard_params)
        cur.execute(sql + ";", tuple(params))
        if cur.rowcount != 1:
            # Idempotent replay of an already-committed exact transition is
            # safe only when the caller's expected state is no longer present.
            cur.execute("SELECT current_step FROM applications WHERE id=%s;", (application_id,))
            row = cur.fetchone()
            if allow_already_target and row and str(row[0]) == to:
                return TransitionResult(application_id, expected_from, to, False)
            raise PipelineStateError(
                f"Application state/ownership changed during {expected_from!r} -> {to!r}."
            )
        cur.execute(
            """INSERT INTO pipeline_events(application_id,from_step,to_step,actor,reason,detail_json)
               VALUES (%s,%s,%s,%s,%s,%s);""",
            (application_id, expected_from, to, actor, reason, Jsonb(dict(detail or {}))),
        )
        return TransitionResult(application_id, expected_from, to, True)


DEFAULT_PIPELINE_STATE_STORE = PipelineStateStore()
