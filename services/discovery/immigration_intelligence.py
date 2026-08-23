"""Explainable immigration-fit synthesis for captured job descriptions.

Job policy, candidate-confirmed facts, and employer evidence remain separate.
E-Verify is relevant to STEM OPT but never proves H-1B sponsorship; historical
H-1B filings are also evidence, not a promise for a particular role.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from services.common.immigration_semantics import (
    ImmigrationAssessment,
    RestrictionType,
    assess_jd_immigration_policy,
)


def normalise_employer_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


def synthesize_immigration_fit(
    profile: Mapping[str, Any] | None,
    policy: ImmigrationAssessment,
    *,
    everify_status: str = "unknown",
    h1b_history_status: str = "unknown",
) -> tuple[str, str]:
    """Return a conservative rank and a plain-language explanation.

    ``HIGH`` means that the available evidence is strong, never that an
    employer promises a visa. An unconfirmed candidate profile never reaches
    HIGH, and citizenship/US-person rules never pass through sponsorship logic.
    """
    profile = profile or {}
    confirmed = bool(profile.get("user_confirmed_at"))
    status = str(profile.get("current_status") or "").casefold()
    citizen = str(profile.get("us_citizen") or "unconfirmed").casefold()
    us_person = str(profile.get("us_person") or "unconfirmed").casefold()
    permanent = str(profile.get("permanent_work_authorization") or "unconfirmed").casefold()
    start_sponsorship = str(profile.get("requires_sponsorship_to_start") or "unconfirmed").casefold()
    future_sponsorship = str(profile.get("requires_future_sponsorship") or "unconfirmed").casefold()
    kind = policy.restriction_type

    if kind is RestrictionType.US_CITIZENSHIP:
        if confirmed and citizen == "no":
            return "BLOCKED", "JD requires U.S. citizenship, which conflicts with the candidate-confirmed profile."
        return "LOW", "JD requires U.S. citizenship; this is distinct from sponsorship and requires review."
    if kind is RestrictionType.US_PERSON:
        if confirmed and us_person == "no":
            return "BLOCKED", "JD requires U.S.-person status, which conflicts with the candidate-confirmed profile."
        return "LOW", "JD requires U.S.-person status; this is distinct from sponsorship and requires review."
    if kind is RestrictionType.PERMANENT_AUTHORIZATION:
        if confirmed and (permanent == "no" or status == "f1" or future_sponsorship == "yes"):
            return "BLOCKED", "JD requires permanent/unrestricted authorization, which conflicts with the candidate-confirmed profile."
        return "LOW", "JD requests permanent/unrestricted authorization; confirm the exact requirement before applying."
    if kind is RestrictionType.NO_SPONSORSHIP:
        if confirmed and (start_sponsorship == "yes" or future_sponsorship == "yes"):
            return "BLOCKED", "JD explicitly declines sponsorship and conflicts with the candidate-confirmed sponsorship need."
        return "LOW", "JD explicitly declines sponsorship; no compatibility may be inferred without a confirmed profile."

    employer_positive = everify_status == "verified" and h1b_history_status == "positive"
    policy_compatible = kind in {RestrictionType.OPT_COMPATIBLE, RestrictionType.STEM_OPT_COMPATIBLE}
    if confirmed and employer_positive:
        return (
            "HIGH",
            "JD has no conflicting restriction and the employer has recorded E-Verify and positive H-1B-history evidence; this is evidence, not a sponsorship promise.",
        )
    if policy_compatible or everify_status == "verified" or h1b_history_status == "positive":
        return (
            "POSSIBLE",
            "JD/employer evidence is potentially compatible, but missing or partial evidence means sponsorship is not guaranteed.",
        )
    return "UNKNOWN", "The JD has no compatible employer-policy evidence and employer immigration evidence is incomplete."


def _candidate_profile(cur: Any) -> dict[str, Any]:
    cur.execute(
        """
        SELECT current_status, us_citizen, us_person, permanent_work_authorization,
               requires_sponsorship_to_start, requires_future_sponsorship,
               user_confirmed_at
        FROM immigration_profiles WHERE profile_key = 'primary';
        """
    )
    row = cur.fetchone()
    keys = (
        "current_status", "us_citizen", "us_person", "permanent_work_authorization",
        "requires_sponsorship_to_start", "requires_future_sponsorship", "user_confirmed_at",
    )
    return dict(zip(keys, row)) if row else {}


def _ensure_employer(cur: Any, application_id: str) -> str | None:
    cur.execute("SELECT company FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    company = str(row[0] or "").strip() if row else ""
    normalized = normalise_employer_name(company)
    if not normalized:
        return None
    cur.execute(
        """
        INSERT INTO employers (canonical_name, normalized_name)
        VALUES (%s, %s)
        ON CONFLICT (normalized_name) DO UPDATE SET updated_at = now()
        RETURNING id::text;
        """,
        (company, normalized),
    )
    employer_id = str(cur.fetchone()[0])
    cur.execute(
        """
        INSERT INTO employer_aliases (employer_id, alias_name, normalized_alias)
        VALUES (%s, %s, %s)
        ON CONFLICT (normalized_alias) DO NOTHING;
        """,
        (employer_id, company, normalized),
    )
    cur.execute("UPDATE applications SET employer_id = %s WHERE id = %s;", (employer_id, application_id))
    return employer_id


def _latest_employer_statuses(cur: Any, employer_id: str | None) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if not employer_id:
        return "unknown", "unknown", {}, {}
    cur.execute(
        """
        SELECT DISTINCT ON (evidence_type) evidence_type, status, source_url,
               source_name, observed_at, confidence, legal_entity_name, notes
        FROM employer_immigration_evidence
        WHERE employer_id = %s AND (expires_at IS NULL OR expires_at > now())
        ORDER BY evidence_type, observed_at DESC, created_at DESC;
        """,
        (employer_id,),
    )
    values: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind, status, url, source, observed, confidence, entity, notes in cur.fetchall():
        values[str(kind)] = (str(status), {
            "source_url": url, "source_name": source, "observed_at": str(observed),
            "confidence": float(confidence), "legal_entity_name": entity, "note": notes or "",
        })
    everify = values.get("everify", ("unknown", {}))
    h1b = values.get("h1b_history", ("unknown", {}))
    return everify[0], h1b[0], everify[1], h1b[1]


def record_jd_immigration_assessment(cur: Any, application_id: str, jd_text: str) -> dict[str, Any]:
    """Upsert job policy plus a synthesized fit as soon as a JD is stored."""
    policy = assess_jd_immigration_policy(jd_text)
    employer_id = _ensure_employer(cur, application_id)
    everify, h1b, everify_evidence, h1b_evidence = _latest_employer_statuses(cur, employer_id)
    status, reason = synthesize_immigration_fit(
        _candidate_profile(cur), policy, everify_status=everify, h1b_history_status=h1b,
    )
    cur.execute(
        """
        INSERT INTO application_immigration_assessments
          (application_id, status, restriction_type, jd_policy_result, jd_policy_evidence,
           everify_status, everify_evidence, h1b_history_status, h1b_history_evidence,
           final_reason, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (application_id) DO UPDATE
        SET status = EXCLUDED.status, restriction_type = EXCLUDED.restriction_type,
            jd_policy_result = EXCLUDED.jd_policy_result, jd_policy_evidence = EXCLUDED.jd_policy_evidence,
            everify_status = EXCLUDED.everify_status, everify_evidence = EXCLUDED.everify_evidence,
            h1b_history_status = EXCLUDED.h1b_history_status,
            h1b_history_evidence = EXCLUDED.h1b_history_evidence,
            final_reason = EXCLUDED.final_reason, updated_at = now();
        """,
        (application_id, status, policy.restriction_type.value, policy.jd_policy_result,
         Jsonb(list(policy.evidence)), everify, Jsonb(everify_evidence), h1b,
         Jsonb(h1b_evidence), reason),
    )
    return {
        "status": status, "restriction_type": policy.restriction_type.value,
        "jd_policy_result": policy.jd_policy_result, "jd_policy_evidence": list(policy.evidence),
        "everify_status": everify, "h1b_history_status": h1b, "final_reason": reason,
    }


def record_employer_evidence(
    cur: Any, *, application_id: str, kind: str, status: str, source_url: str,
    source_name: str, note: str = "", confidence: float = 0.7,
    legal_entity_name: str | None = None,
) -> int:
    """Append sourced employer evidence and recompute every matching captured JD."""
    employer_id = _ensure_employer(cur, application_id)
    if not employer_id:
        raise ValueError("Application has no company name to associate with employer evidence.")
    cur.execute(
        """
        INSERT INTO employer_immigration_evidence
          (employer_id, evidence_type, status, source_name, source_url,
           legal_entity_name, confidence, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (employer_id, kind, status, source_name, source_url, legal_entity_name, confidence, note),
    )
    cur.execute("SELECT id::text, jd_text FROM applications WHERE employer_id = %s AND jd_text IS NOT NULL;", (employer_id,))
    applications = cur.fetchall()
    for other_id, other_jd in applications:
        record_jd_immigration_assessment(cur, str(other_id), str(other_jd or ""))
    return len(applications)
