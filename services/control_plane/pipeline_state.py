"""The canonical transactional application-state mutation boundary.

Migration 088 moves the final compare-and-swap and audit insertion into
PostgreSQL. ``pipeline_transitions`` remains the legal-edge registry and
``pipeline_events`` remains the immutable transition ledger. Python still
evaluates caller-specific guards, but the database owns the version token,
legal edge classification, state mutation, and event.
"""
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
    pipeline_version: int | None = None


class PipelineStateStore:
    """Validate caller guards, then invoke the DB-authoritative transition."""

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
        idempotency_key: str | None = None,
        workflow_run_id: str | None = None,
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

        # Lock the application while evaluating dynamic caller guards.  The DB
        # function locks it again in the same transaction and checks this exact
        # version, closing the ABA/re-entry hole that current_step-only CAS has.
        sql = (
            "SELECT current_step,pipeline_version FROM applications "
            "WHERE id=%s FOR UPDATE"
        )
        cur.execute(sql + ";", (application_id,))
        row = cur.fetchone()
        if not row:
            raise PipelineStateError(f"Application not found: {application_id}")
        current_step, pipeline_version = str(row[0] or ""), int(row[1] or 0)
        if current_step != expected_from:
            if allow_already_target and current_step == to:
                # "Already there" is safe only when the current pipeline
                # incarnation was produced by this exact edge. Merely seeing
                # the same step is an ABA bug if the application left and later
                # returned. When an idempotency key exists, require that exact
                # committed event as well.
                if idempotency_key:
                    cur.execute(
                        """SELECT pipeline_version FROM pipeline_events
                             WHERE application_id=%s AND idempotency_key=%s
                               AND from_step=%s AND to_step=%s;""",
                        (application_id, idempotency_key, expected_from, to),
                    )
                else:
                    cur.execute(
                        """SELECT pipeline_version FROM pipeline_events
                             WHERE application_id=%s AND from_step=%s AND to_step=%s
                             ORDER BY pipeline_version DESC LIMIT 1;""",
                        (application_id, expected_from, to),
                    )
                committed = cur.fetchone()
                if committed and int(committed[0] or -1) == pipeline_version:
                    return TransitionResult(application_id, expected_from, to, False, pipeline_version)
            raise PipelineStateError(
                f"Application state/ownership changed during {expected_from!r} -> {to!r}."
            )

        predicates = ["id=%s", "current_step=%s", "pipeline_version=%s"]
        params: list[Any] = [application_id, expected_from, pipeline_version]
        if lease_run_id:
            predicates.append("processing_run_id=%s::uuid AND processing_step=%s AND processing_lease_expires_at>now()")
            params.extend([lease_run_id, expected_from])
        if expected_job_url is not None:
            predicates.append("coalesce(job_url,'')=%s")
            params.append(expected_job_url)
        if expected_jd_hash is not None:
            predicates.append("coalesce(jd_hash,'')=%s")
            params.append(expected_jd_hash)
        if guard_sql:
            predicates.append(f"({guard_sql})")
            params.extend(guard_params)
        # Keep the state CAS predicate explicit; pipeline_version extends the
        # historical WHERE id=%s AND current_step=%s guard to close ABA/re-entry.
        base_guard = "SELECT 1 FROM applications WHERE id=%s AND current_step=%s AND pipeline_version=%s"
        extra_predicates = predicates[3:]
        cur.execute(base_guard + (" AND " + " AND ".join(extra_predicates) if extra_predicates else "") + ";",
                    tuple(params))
        if not cur.fetchone():
            raise PipelineStateError(
                f"Application state/ownership/guard changed during {expected_from!r} -> {to!r}."
            )

        kind = required_kind
        if kind is None and require_automated is True:
            kind = "automated"
        elif kind is None and require_automated is False:
            kind = "human"
        elif kind is None:
            kind = edge_kind
        try:
            cur.execute(
                """SELECT jobos_transition_application(
                       %s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,%s,%s,%s,%s::uuid
                   );""",
                (
                    application_id, pipeline_version, expected_from, to, actor, reason,
                    Jsonb(dict(detail or {})), status, kind, lease_run_id,
                    expected_job_url, expected_jd_hash, idempotency_key, workflow_run_id,
                ),
            )
            new_version = int(cur.fetchone()[0])
        except Exception as exc:
            raise PipelineStateError(str(exc)) from exc
        return TransitionResult(application_id, expected_from, to, True, new_version)


DEFAULT_PIPELINE_STATE_STORE = PipelineStateStore()
