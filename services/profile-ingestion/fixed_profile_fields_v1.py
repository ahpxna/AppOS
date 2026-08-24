#!/usr/bin/env python3
"""User-verified fixed resume fields, document suggestions, and certification lifecycle.

Project implementation wording is dynamic and may be refreshed from GitHub.
Identity, education, GPA-display decisions, contact data, certifications and the
fixed template zone are not.  Parsed documents may *suggest* values, but only a
user/official verification makes them resume-eligible.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PARSED_ROOT = ROOT / "data/profile_parsed_v2"
sys.path.insert(0, str(ROOT))

from services.common.fixed_profile_policy import (
    FIELD_BY_KEY,
    FIELD_DEFINITIONS,
    FieldDefinition,
    normalize_bool,
    normalize_value,
    readiness_from_records,
)

EXTRACTOR_VERSION = "fixed_profile_document_suggestions_v1_2026_08_24"
VERIFIED = {"user_verified", "document_verified"}


def display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _load_db():
    import psycopg
    from psycopg.types.json import Jsonb
    from services.common.config import database_dsn
    return psycopg, Jsonb, database_dsn


def _fetch_state(cur) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    cur.execute(
        """
        SELECT field_key, value_json, display_value, verification_status,
               show_on_resume, verified_by, verified_at, expires_at
        FROM candidate_fixed_fields ORDER BY field_key;
        """
    )
    fields = {
        row[0]: {
            "value": row[1], "display_value": row[2], "verification_status": row[3],
            "show_on_resume": row[4], "verified_by": row[5], "verified_at": row[6], "expires_at": row[7],
        }
        for row in cur.fetchall()
    }
    cur.execute(
        """
        SELECT id::text, name, issuer, certification_status, earned_at, expires_at,
               credential_id, credential_url, show_on_resume, verification_status,
               source_revision_id::text, notes
        FROM candidate_certifications ORDER BY lower(name), lower(coalesce(issuer,''));
        """
    )
    certs = [
        {"id": r[0], "name": r[1], "issuer": r[2], "certification_status": r[3],
         "earned_at": r[4], "expires_at": r[5], "credential_id": r[6], "credential_url": r[7],
         "show_on_resume": r[8], "verification_status": r[9], "source_revision_id": r[10], "notes": r[11]}
        for r in cur.fetchall()
    ]
    return fields, certs


def _fetch_suggestions(cur, *, pending_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE s.status='pending'" if pending_only else ""
    cur.execute(
        f"""
        SELECT s.id::text, s.field_key, s.suggested_value_json, s.suggested_display_value,
               s.source_revision_id::text, s.confidence, s.conflicts_current, s.status,
               d.logical_source_key, r.embedded_created_at, r.embedded_modified_at,
               r.filesystem_modified_at
        FROM candidate_fixed_field_suggestions s
        JOIN profile_source_revisions r ON r.id=s.source_revision_id
        JOIN profile_source_documents d ON d.id=r.source_document_id
        {where}
        ORDER BY s.conflicts_current DESC, d.logical_source_key, s.field_key, s.created_at DESC;
        """
    )
    return [
        {"id": r[0], "field_key": r[1], "value": r[2], "display_value": r[3],
         "source_revision_id": r[4], "confidence": float(r[5]), "conflicts_current": r[6],
         "status": r[7], "logical_source_key": r[8], "embedded_created_at": r[9],
         "embedded_modified_at": r[10], "filesystem_modified_at": r[11]}
        for r in cur.fetchall()
    ]


def status() -> dict[str, Any]:
    psycopg, _, database_dsn = _load_db()
    with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        fields, certs = _fetch_state(cur)
        suggestions = _fetch_suggestions(cur)
    return {
        "readiness": readiness_from_records(fields, certs),
        "fields": fields,
        "certifications": certs,
        "pending_document_suggestions": suggestions,
        "pending_conflicts": [s for s in suggestions if s["conflicts_current"]],
    }


def set_field(key: str, raw_value: Any, *, actor: str, source_revision_id: str | None,
              show_on_resume: bool | None, apply: bool) -> dict[str, Any]:
    if key not in FIELD_BY_KEY:
        raise ValueError(f"Unknown fixed field: {key}")
    definition = FIELD_BY_KEY[key]
    value = normalize_value(definition, raw_value)
    shown = definition.show_on_resume_default if show_on_resume is None else bool(show_on_resume)
    psycopg, Jsonb, database_dsn = _load_db()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT value_json, verification_status, source_revision_id FROM candidate_fixed_fields WHERE field_key=%s FOR UPDATE", (key,))
        previous = cur.fetchone()
        if previous:
            cur.execute(
                """
                INSERT INTO candidate_fixed_field_history
                  (field_key, value_json, verification_status, changed_by, source_revision_id)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (key, Jsonb(previous[0]), previous[1], actor, previous[2]),
            )
        cur.execute(
            """
            INSERT INTO candidate_fixed_fields
              (field_key, field_group, value_json, display_value, mode, verification_status,
               verified_by, verified_at, source_revision_id, show_on_resume, updated_at)
            VALUES (%s,%s,%s,%s,'fixed','user_verified',%s,now(),%s,%s,now())
            ON CONFLICT (field_key)
            DO UPDATE SET field_group=EXCLUDED.field_group,
                          value_json=EXCLUDED.value_json,
                          display_value=EXCLUDED.display_value,
                          mode='fixed', verification_status='user_verified',
                          verified_by=EXCLUDED.verified_by, verified_at=now(),
                          source_revision_id=EXCLUDED.source_revision_id,
                          show_on_resume=EXCLUDED.show_on_resume, updated_at=now();
            """,
            (key, definition.group, Jsonb(value), display_value(value), actor, source_revision_id, shown),
        )
        if definition.applicant_identity_key and display_value(value).strip():
            cur.execute(
                """
                INSERT INTO applicant_identity (field_name, field_value, field_group, approved, notes, updated_at)
                VALUES (%s,%s,%s,true,'Synced from user-verified candidate_fixed_fields.',now())
                ON CONFLICT (field_name)
                DO UPDATE SET field_value=EXCLUDED.field_value, approved=true,
                              notes=EXCLUDED.notes, updated_at=now();
                """,
                (definition.applicant_identity_key, display_value(value), definition.group),
            )
        # This explicit user decision resolves any pending suggestions for the
        # same field.  Equal values are accepted; different values are rejected.
        cur.execute(
            """
            UPDATE candidate_fixed_field_suggestions
            SET status=CASE WHEN lower(trim(suggested_display_value))=lower(trim(%s))
                            THEN 'accepted' ELSE 'rejected' END,
                conflicts_current=false, updated_at=now()
            WHERE field_key=%s AND status='pending';
            """,
            (display_value(value), key),
        )
        result = {"field_key": key, "value": value, "show_on_resume": shown,
                  "verification_status": "user_verified", "committed": apply}
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return result


def add_certification(*, name: str, issuer: str, status_value: str, earned_at: str | None,
                      expires_at: str | None, credential_id: str | None, credential_url: str | None,
                      show_on_resume: bool, actor: str, source_revision_id: str | None = None,
                      verification_status: str = "user_verified", apply: bool) -> dict[str, Any]:
    status_value = status_value.strip().casefold()
    allowed = {"planned", "studying", "scheduled", "earned", "expired", "revoked", "excluded"}
    if status_value not in allowed:
        raise ValueError("Invalid certification status.")
    if show_on_resume and status_value != "earned":
        raise ValueError("Only earned certifications may be marked for resume display.")
    if verification_status not in {"candidate", "document_verified", "user_verified", "conflict", "expired", "excluded"}:
        raise ValueError("Invalid certification verification status.")
    earned_date = date.fromisoformat(earned_at) if earned_at else None
    expiry_date = date.fromisoformat(expires_at) if expires_at else None
    psycopg, _, database_dsn = _load_db()
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO candidate_certifications
              (name, issuer, certification_status, earned_at, expires_at, credential_id,
               credential_url, show_on_resume, verification_status, source_revision_id,
               verified_by, verified_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    CASE WHEN %s IN ('user_verified','document_verified') THEN now() ELSE NULL END,now())
            ON CONFLICT (name, issuer)
            DO UPDATE SET certification_status=EXCLUDED.certification_status,
                          earned_at=COALESCE(EXCLUDED.earned_at,candidate_certifications.earned_at),
                          expires_at=COALESCE(EXCLUDED.expires_at,candidate_certifications.expires_at),
                          credential_id=COALESCE(EXCLUDED.credential_id,candidate_certifications.credential_id),
                          credential_url=COALESCE(EXCLUDED.credential_url,candidate_certifications.credential_url),
                          show_on_resume=EXCLUDED.show_on_resume,
                          verification_status=EXCLUDED.verification_status,
                          source_revision_id=COALESCE(EXCLUDED.source_revision_id,candidate_certifications.source_revision_id),
                          verified_by=EXCLUDED.verified_by,
                          verified_at=EXCLUDED.verified_at, updated_at=now()
            RETURNING id::text;
            """,
            (name.strip(), issuer.strip(), status_value, earned_date, expiry_date,
             credential_id or None, credential_url or None, show_on_resume, verification_status,
             source_revision_id, actor if verification_status != "candidate" else None, verification_status),
        )
        cert_id = cur.fetchone()[0]
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {"certification_id": cert_id, "name": name, "status": status_value,
            "verification_status": verification_status, "show_on_resume": show_on_resume, "committed": apply}


# ---------------------------------------------------------------------------
# Conservative document suggestions

_LABEL_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("personal.full_name", re.compile(r"^(?:full\s*name|name)\s*[:\-]\s*(.+)$", re.I), 0.75),
    ("personal.email", re.compile(r"^(?:e-?mail)\s*[:\-]\s*([^\s]+@[^\s]+)$", re.I), 0.90),
    ("personal.phone", re.compile(r"^(?:phone|mobile|telephone)\s*[:\-]\s*(.+)$", re.I), 0.80),
    ("resume.location", re.compile(r"^(?:location|address)\s*[:\-]\s*(.+)$", re.I), 0.70),
    ("resume.linkedin_url", re.compile(r"^(?:linkedin)\s*[:\-]\s*(https?://\S+)$", re.I), 0.95),
    ("resume.github_url", re.compile(r"^(?:github)\s*[:\-]\s*(https?://\S+)$", re.I), 0.95),
    ("education.university", re.compile(r"^(?:university|institution)\s*[:\-]\s*(.+)$", re.I), 0.85),
    ("education.degree", re.compile(r"^(?:degree|qualification)\s*[:\-]\s*(.+)$", re.I), 0.85),
    ("education.major", re.compile(r"^(?:major|subject|field\s+of\s+study)\s*[:\-]\s*(.+)$", re.I), 0.80),
    ("education.graduation_date", re.compile(r"^(?:graduation|completion)\s*(?:date)?\s*[:\-]\s*(\d{4}-\d{2}(?:-\d{2})?)$", re.I), 0.90),
)
_GPA_PATTERN = re.compile(
    r"\b(?:GPA|grade\s+point\s+average)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:/|out\s+of)\s*(\d+(?:\.\d+)?)\b",
    re.I,
)
_GPA_VALUE_PATTERN = re.compile(r"^\s*(?:GPA|grade\s+point\s+average)\s*[:\-]\s*(\d+(?:\.\d+)?)\s*$", re.I)
_GPA_SCALE_PATTERN = re.compile(r"^\s*(?:GPA\s+scale|scale)\s*[:\-]\s*(\d+(?:\.\d+)?)\s*$", re.I)
_CERT_PATTERN = re.compile(r"^(?:certification|certificate)\s*[:\-]\s*(.+?)\s*$", re.I)


def candidate_suggestions_from_text(text: str) -> dict[str, Any]:
    """Extract only explicit labelled values; never infer them from prose."""
    field_candidates: list[dict[str, Any]] = []
    certifications: list[str] = []
    lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines() if line.strip()]
    seen: set[tuple[str, str]] = set()
    gpa_value: str | None = None
    gpa_scale: str | None = None
    for line in lines:
        paired = _GPA_PATTERN.search(line)
        if paired:
            gpa_value, gpa_scale = paired.group(1), paired.group(2)
        else:
            single = _GPA_VALUE_PATTERN.match(line)
            if single:
                gpa_value = single.group(1)
            scale = _GPA_SCALE_PATTERN.match(line)
            if scale:
                gpa_scale = scale.group(1)
        cert = _CERT_PATTERN.match(line)
        if cert and 2 <= len(cert.group(1)) <= 180:
            certifications.append(cert.group(1).strip())
        for key, pattern, confidence in _LABEL_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            value = match.group(1).strip().rstrip(".,;")
            if not value:
                continue
            marker = (key, value.casefold())
            if marker not in seen:
                seen.add(marker)
                field_candidates.append({"field_key": key, "value": value, "confidence": confidence})
    if gpa_value:
        field_candidates.append({"field_key": "education.gpa.value", "value": gpa_value, "confidence": 0.95})
    if gpa_scale:
        field_candidates.append({"field_key": "education.gpa.scale", "value": gpa_scale, "confidence": 0.95})
    return {"fields": field_candidates, "certifications": sorted(set(certifications))}


def _current_source_revisions(cur) -> dict[str, str]:
    cur.execute(
        """
        SELECT d.logical_source_key, d.current_revision_id::text
        FROM profile_source_documents d
        JOIN profile_source_revisions r ON r.id=d.current_revision_id
        WHERE d.status='active' AND d.authority_class='official_document'
          AND r.status='current';
        """
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def suggest_from_parsed_files(*, apply: bool) -> dict[str, Any]:
    """Record candidate values from current official document revisions.

    Suggestions are deliberately non-authoritative.  A differing value becomes
    a pending conflict that blocks fixed-field readiness until the user chooses
    the canonical value in the wizard.
    """
    psycopg, Jsonb, database_dsn = _load_db()
    result: list[dict[str, Any]] = []
    cert_result: list[dict[str, Any]] = []
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        revision_by_key = _current_source_revisions(cur)
        cur.execute("SELECT field_key, display_value, verification_status FROM candidate_fixed_fields")
        current_fields = {row[0]: {"display": row[1] or "", "status": row[2]} for row in cur.fetchall()}
        for parsed in sorted((PARSED_ROOT / "00_official").rglob("*.txt")) if (PARSED_ROOT / "00_official").exists() else []:
            rel = parsed.relative_to(PARSED_ROOT).with_suffix("")
            # Parsed sidecars correspond to .docx/.pdf/.md/.txt; find current
            # logical source by matching the path stem instead of guessing suffix.
            candidates = [
                (key, rev) for key, rev in revision_by_key.items()
                if Path(key.removeprefix("profile_sources_v2/")).with_suffix("") == rel
            ]
            if len(candidates) != 1:
                continue
            logical_key, source_revision_id = candidates[0]
            extracted = candidate_suggestions_from_text(parsed.read_text(encoding="utf-8", errors="replace"))
            for item in extracted["fields"]:
                key = item["field_key"]
                definition = FIELD_BY_KEY.get(key)
                if not definition:
                    continue
                try:
                    normalized = normalize_value(definition, item["value"])
                except ValueError:
                    continue
                shown = display_value(normalized)
                current = current_fields.get(key)
                conflict = bool(current and current["status"] in VERIFIED and current["display"].strip().casefold() != shown.strip().casefold())
                result.append({"field_key": key, "value": normalized, "display_value": shown,
                               "source_revision_id": source_revision_id, "logical_source_key": logical_key,
                               "confidence": item["confidence"], "conflicts_current": conflict})
                cur.execute(
                    """
                    UPDATE candidate_fixed_field_suggestions s
                    SET status='superseded', updated_at=now()
                    FROM profile_source_revisions old_r, profile_source_revisions current_r
                    WHERE s.source_revision_id=old_r.id
                      AND current_r.id=%s
                      AND old_r.source_document_id=current_r.source_document_id
                      AND old_r.id<>current_r.id
                      AND s.field_key=%s AND s.status='pending';
                    """,
                    (source_revision_id, key),
                )
                cur.execute(
                    """
                    INSERT INTO candidate_fixed_field_suggestions
                      (field_key, suggested_value_json, suggested_display_value, source_revision_id,
                       confidence, conflicts_current, status, extractor_version)
                    VALUES (%s,%s,%s,%s,%s,%s,'pending',%s)
                    ON CONFLICT (field_key, source_revision_id, suggested_display_value)
                    DO UPDATE SET confidence=EXCLUDED.confidence,
                                  conflicts_current=EXCLUDED.conflicts_current,
                                  status=CASE WHEN candidate_fixed_field_suggestions.status='superseded'
                                              THEN 'pending' ELSE candidate_fixed_field_suggestions.status END,
                                  updated_at=now();
                    """,
                    (key, Jsonb(normalized), shown, source_revision_id, item["confidence"], conflict, EXTRACTOR_VERSION),
                )
            for cert_name in extracted["certifications"]:
                cert_result.append({"name": cert_name, "source_revision_id": source_revision_id,
                                    "logical_source_key": logical_key})
                cur.execute(
                    """
                    INSERT INTO candidate_certifications
                      (name, issuer, certification_status, show_on_resume, verification_status,
                       source_revision_id, notes, updated_at)
                    VALUES (%s,'','planned',false,'candidate',%s,%s,now())
                    ON CONFLICT (name, issuer)
                    DO UPDATE SET source_revision_id=EXCLUDED.source_revision_id,
                                  notes=CASE WHEN candidate_certifications.verification_status='candidate'
                                             THEN EXCLUDED.notes ELSE candidate_certifications.notes END,
                                  updated_at=now();
                    """,
                    (cert_name, source_revision_id,
                     f"Candidate certification label parsed from {logical_key}; user confirmation required."),
                )
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {"field_suggestions": result, "certification_suggestions": cert_result,
            "extractor_version": EXTRACTOR_VERSION, "committed": apply}


def _ask(definition: FieldDefinition, current: str | None = None, suggestion: str | None = None) -> str:
    default = suggestion if suggestion is not None else current
    suffix = f" [{default}]" if default else ""
    return input(f"{definition.prompt}{suffix}: ").strip() or (default or "")


def _resolve_suggestion(cur, suggestion_id: str, accepted: bool) -> None:
    cur.execute(
        "UPDATE candidate_fixed_field_suggestions SET status=%s, conflicts_current=false, updated_at=now() WHERE id=%s",
        ("accepted" if accepted else "rejected", suggestion_id),
    )


def wizard(*, actor: str, apply: bool) -> int:
    # Refresh deterministic document candidates first; this never approves them.
    suggest_from_parsed_files(apply=apply)
    snapshot = status()
    fields = snapshot["fields"]
    suggestions_by_key: dict[str, list[dict[str, Any]]] = {}
    for suggestion in snapshot["pending_document_suggestions"]:
        suggestions_by_key.setdefault(suggestion["field_key"], []).append(suggestion)

    print("Fixed resume fields. Parsed documents are suggestions only; Enter accepts the shown default.\n")
    for definition in FIELD_DEFINITIONS:
        current = fields.get(definition.key, {})
        current_display = current.get("display_value")
        candidates = suggestions_by_key.get(definition.key, [])
        suggestion = candidates[0] if candidates else None
        suggestion_display = suggestion.get("display_value") if suggestion else None
        if suggestion:
            print(f"  document suggestion for {definition.key}: {suggestion_display!r}"
                  f" from {suggestion['logical_source_key']}"
                  + (" [CONFLICTS WITH VERIFIED VALUE]" if suggestion["conflicts_current"] else ""))

        if current.get("verification_status") in VERIFIED:
            if suggestion and suggestion["conflicts_current"]:
                choice = input(
                    f"Verified value is [{current_display}] but the document suggests [{suggestion_display}]. "
                    "Enter=keep verified, a=accept document suggestion, e=enter another value: "
                ).strip().casefold()
                if choice in {"", "k", "keep"}:
                    if apply:
                        psycopg, _, database_dsn = _load_db()
                        with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                            _resolve_suggestion(cur, suggestion["id"], accepted=False)
                    continue
                if choice in {"a", "accept"}:
                    value = suggestion_display or ""
                elif choice in {"e", "edit"}:
                    value = _ask(definition, None if current_display is None else str(current_display), None)
                else:
                    print("Unrecognized choice; keeping the verified value.")
                    continue
            else:
                answer = input(f"{definition.prompt} [{current_display}] (Enter=keep, !=change): ").strip()
                if answer != "!":
                    continue
                value = _ask(definition, None if current_display is None else str(current_display), None)
        else:
            value = _ask(definition, None if current_display is None else str(current_display), suggestion_display)
        if not value and not definition.required:
            if suggestion and apply:
                psycopg, _, database_dsn = _load_db()
                with psycopg.connect(database_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                    _resolve_suggestion(cur, suggestion["id"], accepted=False)
            continue
        set_field(definition.key, value, actor=actor,
                  source_revision_id=suggestion.get("source_revision_id") if suggestion else None,
                  show_on_resume=None, apply=apply)

    latest = status()
    candidate_certs = [c for c in latest["certifications"] if c["verification_status"] == "candidate"]
    for cert in candidate_certs:
        print(f"\nCertification candidate from document: {cert['name']}")
        keep = normalize_bool(input("Track/confirm this certification? (yes/no): "))
        if not keep:
            continue
        cert_status = input("Status [earned/studying/scheduled/planned/expired/revoked]: ").strip() or "earned"
        issuer = input(f"Issuer [{cert.get('issuer') or ''}]: ").strip() or (cert.get("issuer") or "")
        earned = input("Earned date YYYY-MM-DD (optional): ").strip() or None
        expires = input("Expiry date YYYY-MM-DD (optional): ").strip() or None
        show = normalize_bool(input("Show on resume? (yes/no): "))
        add_certification(name=cert["name"], issuer=issuer, status_value=cert_status, earned_at=earned,
                          expires_at=expires, credential_id=cert.get("credential_id"),
                          credential_url=cert.get("credential_url"), show_on_resume=show, actor=actor,
                          source_revision_id=cert.get("source_revision_id"), verification_status="user_verified", apply=apply)

    reviewed = status()["fields"].get("certifications.reviewed", {})
    if reviewed.get("verification_status") not in VERIFIED:
        more = normalize_bool(input("Do you have any other certifications JobOS should track? (yes/no): "))
        while more:
            name = input("Certification name (blank to finish): ").strip()
            if not name:
                break
            issuer = input("Issuer: ").strip()
            cert_status = input("Status [earned/studying/scheduled/planned/expired/revoked]: ").strip() or "earned"
            earned = input("Earned date YYYY-MM-DD (optional): ").strip() or None
            expires = input("Expiry date YYYY-MM-DD (optional): ").strip() or None
            show = normalize_bool(input("Show on resume? (yes/no): "))
            add_certification(name=name, issuer=issuer, status_value=cert_status, earned_at=earned,
                              expires_at=expires, credential_id=None, credential_url=None,
                              show_on_resume=show, actor=actor, apply=apply)
            more = normalize_bool(input("Add another certification? (yes/no): "))
        set_field("certifications.reviewed", True, actor=actor, source_revision_id=None,
                  show_on_resume=False, apply=apply)
    print(json.dumps(status(), indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage user-verified fixed resume fields.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sugg = sub.add_parser("suggest")
    sugg.add_argument("--apply", action="store_true")
    wiz = sub.add_parser("wizard")
    wiz.add_argument("--actor", default="candidate")
    wiz.add_argument("--apply", action="store_true")
    setter = sub.add_parser("set")
    setter.add_argument("field_key", choices=sorted(FIELD_BY_KEY))
    setter.add_argument("value")
    setter.add_argument("--actor", default="candidate")
    setter.add_argument("--source-revision-id")
    setter.add_argument("--show-on-resume", choices=("yes", "no"))
    setter.add_argument("--apply", action="store_true")
    cert = sub.add_parser("cert-add")
    cert.add_argument("--name", required=True)
    cert.add_argument("--issuer", default="")
    cert.add_argument("--status", required=True)
    cert.add_argument("--earned-at")
    cert.add_argument("--expires-at")
    cert.add_argument("--credential-id")
    cert.add_argument("--credential-url")
    cert.add_argument("--show-on-resume", action="store_true")
    cert.add_argument("--actor", default="candidate")
    cert.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.command == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if args.command == "suggest":
        print(json.dumps(suggest_from_parsed_files(apply=args.apply), indent=2, default=str))
        return 0
    if args.command == "wizard":
        return wizard(actor=args.actor, apply=args.apply)
    if args.command == "set":
        show = None if args.show_on_resume is None else args.show_on_resume == "yes"
        print(json.dumps(set_field(args.field_key, args.value, actor=args.actor,
                                   source_revision_id=args.source_revision_id, show_on_resume=show,
                                   apply=args.apply), indent=2, default=str))
        return 0
    print(json.dumps(add_certification(name=args.name, issuer=args.issuer, status_value=args.status,
                                       earned_at=args.earned_at, expires_at=args.expires_at,
                                       credential_id=args.credential_id, credential_url=args.credential_url,
                                       show_on_resume=args.show_on_resume, actor=args.actor,
                                       verification_status="user_verified", apply=args.apply),
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
