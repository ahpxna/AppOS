import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Dict, List

import psycopg
from psycopg.types.json import Jsonb

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn  # noqa: E402


DSN = database_dsn()

VERSION = "profile_document_map_quality_gate_v1_2026_04_27"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def add_finding(findings: List[Dict], code: str, severity: str, message: str, evidence: str = ""):
    findings.append(
        {
            "code": code,
            "severity": severity,
            "message": message,
            "evidence": evidence[:500] if evidence else "",
        }
    )


def audit_row(row: Dict) -> Dict:
    summary = norm(row.get("document_summary"))
    file_name = norm(row.get("file_name"))
    document_type = row.get("document_type")
    source_role = row.get("source_role")
    risk_notes = row.get("risk_notes") or []
    if not isinstance(risk_notes, list):
        risk_notes = []

    all_text = " ".join([summary, " ".join(norm(x) for x in risk_notes)])
    findings: List[Dict] = []

    flags = {
        "has_document_type_mismatch": False,
        "has_external_metadata_hallucination": False,
        "has_source_role_violation": False,
        "has_guidance_truth_violation": False,
        "has_source_paper_truth_violation": False,
        "has_research_completion_overclaim": False,
        "has_generic_or_low_value_summary": False,
        "has_duplicate_risk_notes": False,
    }

    # 1. Hard document_type mismatch checks.
    if document_type == "course_profile" and "project profile" in summary:
        flags["has_document_type_mismatch"] = True
        add_finding(
            findings,
            "course_profile_called_project_profile",
            "high",
            "A course_profile was summarized as a project profile.",
            row.get("document_summary") or "",
        )

    if document_type == "project_profile" and "course profile" in summary and "project" not in summary:
        flags["has_document_type_mismatch"] = True
        add_finding(
            findings,
            "project_profile_called_course_profile",
            "medium",
            "A project_profile may have been reframed as a course profile.",
            row.get("document_summary") or "",
        )

    # 2. Known false external metadata / recognition patterns.
    external_terms = [
        "neurips",
        "published paper",
        "peer-reviewed",
        "conference paper",
        "journal article",
        "award-winning",
        "industry-recognized",
    ]
    for term in external_terms:
        if term in all_text:
            flags["has_external_metadata_hallucination"] = True
            add_finding(
                findings,
                "external_metadata_possible_hallucination",
                "high",
                f"Mapped document contains external-recognition metadata term: {term}.",
                row.get("document_summary") or "",
            )

    # 3. Source role violations.
    if source_role == "course_reference_material":
        bad_terms = [
            "user implemented",
            "user designed",
            "demonstrates user's",
            "showcases user's",
            "my project",
            "hands-on project",
        ]
        if any(t in all_text for t in bad_terms):
            flags["has_source_paper_truth_violation"] = True
            flags["has_source_role_violation"] = True
            add_finding(
                findings,
                "source_paper_used_as_user_truth",
                "high",
                "A source paper/course reading appears to be framed as direct evidence of user work.",
                row.get("document_summary") or "",
            )

    if source_role == "career_strategy_guidance":
        credential_terms = [
            "completed credential",
            "certified",
            "certification earned",
            "qualification achieved",
            "proves the user",
        ]
        if any(t in all_text for t in credential_terms):
            flags["has_guidance_truth_violation"] = True
            flags["has_source_role_violation"] = True
            add_finding(
                findings,
                "guidance_used_as_truth",
                "high",
                "Guidance/planning source appears to be framed as completed credential/profile truth.",
                row.get("document_summary") or "",
            )

    # 4. Research completion overclaim.
    if document_type == "research_profile":
        risky_research_terms = [
            "validated with empirical results",
            "completed empirical results",
            "deployed system",
            "production-ready",
            "real-world deployment",
            "proven to improve",
        ]
        if any(t in all_text for t in risky_research_terms):
            flags["has_research_completion_overclaim"] = True
            add_finding(
                findings,
                "research_completion_overclaim",
                "high",
                "Research profile may imply completed validation/deployment not guaranteed by document type.",
                row.get("document_summary") or "",
            )

    # 5. Database-system specific guard.
    if "cis330" in file_name:
        if any(t in summary for t in ["incident response", "threat detection", "cybersecurity data analysis initiative"]):
            flags["has_document_type_mismatch"] = True
            add_finding(
                findings,
                "cis330_wrong_domain_framing",
                "high",
                "CIS330 database systems map appears reframed as cybersecurity incident/threat-detection project.",
                row.get("document_summary") or "",
            )

        expected_terms = ["database", "sql", "relational", "normalization", "query", "erd", "transaction", "data warehouse"]
        if not any(t in summary for t in expected_terms):
            flags["has_generic_or_low_value_summary"] = True
            add_finding(
                findings,
                "cis330_missing_database_framing",
                "medium",
                "CIS330 summary does not preserve database-system framing.",
                row.get("document_summary") or "",
            )

    # 6. Generic summary.
    generic_phrases = [
        "career-relevant technical execution",
        "toolchains, methodologies, and career-relevant",
        "structured project profile detailing",
    ]
    if len(summary) < 80 or any(p in summary for p in generic_phrases):
        flags["has_generic_or_low_value_summary"] = True
        add_finding(
            findings,
            "generic_or_low_value_summary",
            "medium",
            "Document summary appears generic or low-value for downstream evidence extraction.",
            row.get("document_summary") or "",
        )

    # 7. Duplicate risk notes.
    normalized_notes = [norm(x) for x in risk_notes if norm(x)]
    counts = Counter(normalized_notes)
    duplicates = [note for note, count in counts.items() if count > 1]
    if duplicates:
        flags["has_duplicate_risk_notes"] = True
        add_finding(
            findings,
            "duplicate_risk_notes",
            "low",
            "Risk notes contain exact duplicate entries.",
            "; ".join(duplicates[:5]),
        )

    # 8. Map status.
    severities = [f["severity"] for f in findings]
    if "critical" in severities or "high" in severities:
        audit_status = "block"
        severity = "high" if "critical" not in severities else "critical"
        recommended_action = "review_before_evidence"
    elif "medium" in severities:
        audit_status = "warn"
        severity = "medium"
        recommended_action = "review_before_evidence"
    elif "low" in severities:
        audit_status = "warn"
        severity = "low"
        recommended_action = "allow"
    else:
        audit_status = "pass"
        severity = "low"
        recommended_action = "allow"

    # Source papers and guidance should not become truth, even if mapped cleanly.
    if source_role == "course_reference_material":
        if audit_status == "pass":
            audit_status = "warn"
            severity = "low"
        recommended_action = "ignore_for_truth"
        add_finding(
            findings,
            "reference_only_source",
            "low",
            "Course/source paper may be used as background reference but not direct profile truth.",
            "",
        )

    if source_role == "career_strategy_guidance":
        if audit_status == "pass":
            audit_status = "warn"
            severity = "low"
        recommended_action = "ignore_for_truth"
        add_finding(
            findings,
            "guidance_only_source",
            "low",
            "Guidance source may inform planning but not direct profile truth.",
            "",
        )

    return {
        "audit_status": audit_status,
        "severity": severity,
        "finding_count": len(findings),
        "findings_json": findings,
        "recommended_action": recommended_action,
        **flags,
    }


def fetch_rows(cur, limit: int):
    cur.execute(
        """
        SELECT
          pd.id,
          rf.file_name,
          pd.document_type,
          pd.source_role,
          pd.status,
          pd.document_summary,
          pd.risk_notes
        FROM profile_documents pd
        LEFT JOIN raw_files rf
          ON rf.id = pd.raw_file_id
        WHERE pd.status = 'mapped'
        ORDER BY
          CASE pd.document_type
            WHEN 'official_transcript' THEN 1
            WHEN 'project_profile' THEN 2
            WHEN 'research_profile' THEN 3
            WHEN 'cross_portfolio_mapping' THEN 4
            WHEN 'course_profile' THEN 5
            WHEN 'source_paper' THEN 6
            WHEN 'guidance_not_truth' THEN 7
            ELSE 9
          END,
          rf.file_name
        LIMIT %s;
        """,
        (limit,),
    )

    keys = ["id", "file_name", "document_type", "source_role", "status", "document_summary", "risk_notes"]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    print("===== PROFILE DOCUMENT MAP QUALITY GATE =====")
    print(f"Version: {VERSION}")
    print(f"Mode:    {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Limit:   {args.limit}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_rows(cur, args.limit)

            print(f"Mapped documents selected: {len(rows)}")

            counts = Counter()
            audited = 0

            for row in rows:
                result = audit_row(row)
                counts[result["audit_status"]] += 1

                print("")
                print(f"- {row['file_name']}")
                print(f"  type/status: {row['document_type']} / {row['status']}")
                print(f"  audit:       {result['audit_status']} / {result['severity']} / {result['recommended_action']}")
                print(f"  findings:    {result['finding_count']}")

                for f in result["findings_json"][:5]:
                    print(f"    - [{f['severity']}] {f['code']}: {f['message']}")

                if not args.apply:
                    continue

                cur.execute(
                    """
                    INSERT INTO profile_document_map_audits (
                      profile_document_id,
                      audit_version,
                      audit_method,
                      audit_status,
                      severity,
                      finding_count,
                      findings_json,
                      has_document_type_mismatch,
                      has_external_metadata_hallucination,
                      has_source_role_violation,
                      has_guidance_truth_violation,
                      has_source_paper_truth_violation,
                      has_research_completion_overclaim,
                      has_generic_or_low_value_summary,
                      has_duplicate_risk_notes,
                      recommended_action
                    )
                    VALUES (
                      %s, %s, 'deterministic_document_map_quality_gate',
                      %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s,
                      %s
                    )
                    ON CONFLICT (profile_document_id, audit_version)
                    DO UPDATE SET
                      audit_status = EXCLUDED.audit_status,
                      severity = EXCLUDED.severity,
                      finding_count = EXCLUDED.finding_count,
                      findings_json = EXCLUDED.findings_json,
                      has_document_type_mismatch = EXCLUDED.has_document_type_mismatch,
                      has_external_metadata_hallucination = EXCLUDED.has_external_metadata_hallucination,
                      has_source_role_violation = EXCLUDED.has_source_role_violation,
                      has_guidance_truth_violation = EXCLUDED.has_guidance_truth_violation,
                      has_source_paper_truth_violation = EXCLUDED.has_source_paper_truth_violation,
                      has_research_completion_overclaim = EXCLUDED.has_research_completion_overclaim,
                      has_generic_or_low_value_summary = EXCLUDED.has_generic_or_low_value_summary,
                      has_duplicate_risk_notes = EXCLUDED.has_duplicate_risk_notes,
                      recommended_action = EXCLUDED.recommended_action,
                      created_at = now()
                    """,
                    (
                        row["id"],
                        VERSION,
                        result["audit_status"],
                        result["severity"],
                        result["finding_count"],
                        Jsonb(result["findings_json"]),
                        result["has_document_type_mismatch"],
                        result["has_external_metadata_hallucination"],
                        result["has_source_role_violation"],
                        result["has_guidance_truth_violation"],
                        result["has_source_paper_truth_violation"],
                        result["has_research_completion_overclaim"],
                        result["has_generic_or_low_value_summary"],
                        result["has_duplicate_risk_notes"],
                        result["recommended_action"],
                    ),
                )
                audited += 1

            if args.apply:
                conn.commit()

    print("")
    print("===== SUMMARY =====")
    print(f"Selected: {len(rows)}")
    print(f"Audited:  {audited if args.apply else 0}")
    for k in ["pass", "warn", "block"]:
        print(f"{k}: {counts[k]}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
