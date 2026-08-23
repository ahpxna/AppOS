"""Persist conservative immigration compatibility evidence for captured JDs.

The worker deliberately evaluates only explicit job-description wording.  It
does not call an employer an H-1B sponsor, infer a result from E-Verify, or
produce a candidate-facing legal answer.  External employer evidence can be
added later to the separate evidence columns introduced by migration 050.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from services.common.immigration_semantics import assess_jd_immigration_policy


def candidate_fit_status(cur: Any, policy_status: str) -> tuple[str, str]:
    """Apply only user-confirmed sponsorship needs to explicit JD policy.

    An explicit employer restriction is not automatically a candidate block
    while the candidate profile is unknown.  Conversely, absence of a policy
    remains UNKNOWN, not sponsor-friendly.  This guards against both false
    rejection and false optimism.
    """
    cur.execute(
        """
        SELECT requires_sponsorship_to_start, requires_future_sponsorship,
               user_confirmed_at
        FROM immigration_profiles WHERE profile_key = 'primary';
        """
    )
    row = cur.fetchone()
    if policy_status == "POSSIBLE":
        return "POSSIBLE", "JD explicitly mentions OPT/F-1 compatibility; sponsorship still requires confirmation."
    if policy_status != "BLOCKED":
        return "UNKNOWN", "The JD has no explicit immigration policy evidence; do not infer sponsorship from absence."
    if row and row[2] and (row[0] == "yes" or row[1] == "yes"):
        return "BLOCKED", "JD explicitly restricts sponsorship and conflicts with the candidate-confirmed sponsorship need."
    return "LOW", "JD explicitly restricts sponsorship; candidate profile is not confirmed to need sponsorship, so manual review is required."


def record_jd_immigration_assessment(cur: Any, application_id: str, jd_text: str) -> dict[str, Any]:
    """Upsert explicit JD-policy evidence as soon as the JD enters JobOS.

    This intentionally runs before no-LLM filters and fit analysis, so even a
    rejected job retains accurate market-policy evidence without being treated
    as a sponsorship promise.
    """
    assessment = assess_jd_immigration_policy(jd_text)
    status, final_reason = candidate_fit_status(cur, assessment.status)
    cur.execute(
        """
        INSERT INTO application_immigration_assessments
          (application_id, status, jd_policy_result, jd_policy_evidence,
           everify_status, h1b_history_status, final_reason, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'unknown', 'unknown', %s, now(), now())
        ON CONFLICT (application_id) DO UPDATE
        SET status = EXCLUDED.status,
            jd_policy_result = EXCLUDED.jd_policy_result,
            jd_policy_evidence = EXCLUDED.jd_policy_evidence,
            final_reason = EXCLUDED.final_reason,
            updated_at = now();
        """,
        (
            application_id, status, assessment.jd_policy_result,
            Jsonb(list(assessment.evidence)), final_reason,
        ),
    )
    return {
        "status": status,
        "jd_policy_result": assessment.jd_policy_result,
        "jd_policy_evidence": list(assessment.evidence),
        "final_reason": final_reason,
    }
