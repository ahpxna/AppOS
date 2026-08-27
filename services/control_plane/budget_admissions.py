"""Idempotent daily quota admission keyed by the work subject."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetAdmission:
    admitted: bool
    newly_admitted: bool
    reason: str = ""


def admit(cur, *, task_kind: str, subject_type: str, subject_id: str) -> BudgetAdmission:
    """Reserve one daily quota unit exactly once for this subject.

    The caller holds the same transaction while reading/updating its daily
    budget row. Replays return ``newly_admitted=False`` and must not consume a
    second full-pipeline/browser quota unit.
    """
    cur.execute("SELECT CURRENT_DATE;")
    budget_date = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO budget_admissions(budget_date,task_kind,subject_type,subject_id)
           VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id;""",
        (budget_date, task_kind, subject_type, subject_id),
    )
    return BudgetAdmission(True, bool(cur.fetchone()))
