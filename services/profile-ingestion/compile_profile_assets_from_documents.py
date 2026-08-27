import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn  # noqa: E402


COMPONENT_NAME = "portfolio_asset_compiler"
TASK_TYPE = "compile_source_document_profile_asset"
COMPILER_VERSION = "portfolio_asset_compiler_v1_source_preserving_2026_04_27"

IMPORTANT_SECTION_PATTERNS = [
    "purpose",
    "master tool narrative",
    "high-level categories",
    "project scope",
    "intellectual positioning",
    "research problem",
    "research questions",
    "direct connection",
    "methodology",
    "testbed",
    "results",
    "soft-fail",
    "mitm",
    "portfolio positioning",
    "resume phrase",
    "professional competencies",
    "strongest course themes",
    "career relevance",
    "final strategic positioning",
    "course coverage",
    "knowledge architecture",
    "abstract",
    "introduction",
    "data preparation",
    "hybrid",
    "dynamic programming",
    "result",
]


def clean_text(text: str) -> str:
    text = text.replace("\x0c", "\n")
    text = re.sub(r"<PARSED TEXT FOR PAGE:\s*([^>]+)>", r"\n\n--- PAGE \1 ---\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def first_title(text: str, fallback: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("--- PAGE")]

    title_lines = []
    for ln in lines[:12]:
        if len(ln) <= 2:
            continue
        if re.match(r"^\d+[\.\)]\s+", ln):
            break
        title_lines.append(ln)
        if len(" ".join(title_lines)) > 120:
            break

    title = " ".join(title_lines).strip()
    if not title:
        title = Path(fallback).stem

    title = re.sub(r"\s+", " ", title)
    return title[:220]


def split_sections(text: str) -> List[Tuple[str, str]]:
    lines = text.splitlines()
    sections: List[Tuple[str, List[str]]] = []
    current_title = "Document Opening"
    current_body: List[str] = []

    heading_re = re.compile(
        r"^(\d+(\.\d+)*[\.\)]\s+.+|[IVX]+\.\s+.+|[A-Z][A-Za-z0-9/&,\-\s]{3,90}:?\s*)$"
    )

    for line in lines:
        stripped = line.strip()

        is_heading = False
        if stripped and len(stripped) <= 140:
            if re.match(r"^\d+(\.\d+)*[\.\)]\s+", stripped):
                is_heading = True
            elif re.match(r"^[IVX]+\.\s+[A-Z]", stripped):
                is_heading = True
            elif stripped.lower() in {
                "abstract",
                "introduction",
                "methodology",
                "result",
                "results",
                "conclusion",
            }:
                is_heading = True
            elif any(stripped.lower().startswith(p) for p in [
                "purpose",
                "master tool narrative",
                "high-level categories",
                "project scope",
                "research question",
                "methodology",
                "professional competencies",
                "portfolio positioning",
                "resume phrase",
                "final strategic positioning",
            ]):
                is_heading = True

        if is_heading:
            if current_body:
                sections.append((current_title, "\n".join(current_body).strip()))
            current_title = stripped
            current_body = []
        else:
            current_body.append(line)

    if current_body:
        sections.append((current_title, "\n".join(current_body).strip()))

    return [(t, b) for t, b in sections if b and len(b.strip()) > 80]


def section_score(title: str, body: str) -> int:
    blob = f"{title}\n{body}".lower()
    score = 0
    for pat in IMPORTANT_SECTION_PATTERNS:
        if pat in blob:
            score += 10
    if "resume phrase" in blob:
        score += 8
    if "portfolio positioning" in blob:
        score += 8
    if "what it does" in blob:
        score += 4
    if "source:" in blob:
        score += 3
    if "job" in blob or "role" in blob:
        score += 3
    if len(body) > 1000:
        score += 2
    return score


def pick_asset_type(file_name: str, title: str, text: str, file_role: str) -> str:
    blob = f"{file_name} {title} {text[:3000]}".lower()

    if "tools" in blob and ("framework" in blob or "source mapping" in blob):
        return "tool_workflow_asset"
    if "cyber war" in blob or "strategic profile" in blob:
        return "strategic_course_asset"
    if "research question" in blob and ("testbed" in blob or "methodology" in blob):
        return "research_project_asset"
    if "project" in blob or file_role == "project_artifact_evidence":
        return "project_asset"
    if "course" in blob or "course coverage" in blob:
        return "course_competency_asset"
    return "source_document_asset"


def infer_tags(text: str) -> Tuple[List[str], List[str], List[str], List[str]]:
    blob = text.lower()

    role_families = []
    competency_tags = []
    tool_tags = []
    project_tags = []

    role_keywords = {
        "cybersecurity": ["cybersecurity", "security analyst", "network security", "penetration"],
        "dfir": ["forensic", "incident response", "dfir", "autopsy", "ftk", "redline"],
        "soc": ["soc", "siem", "incident", "log", "triage"],
        "grc": ["governance", "risk", "compliance", "nist", "iso", "controls"],
        "software_engineering": ["software engineering", "python", "java", "sdlc", "testing"],
        "data_analytics": ["sql", "database", "analytics", "pandas", "data modeling"],
        "ai_security_research": ["multi-agent", "reinforcement learning", "causal", "mean-field"],
        "algorithmic_optimization": ["dynamic programming", "greedy", "optimization", "knapsack", "logistics"],
    }

    for tag, kws in role_keywords.items():
        if any(k in blob for k in kws):
            role_families.append(tag)

    competency_keywords = [
        "forensic acquisition", "memory analysis", "endpoint investigation", "network scanning",
        "penetration testing", "web application security", "enterprise controls", "pki",
        "tls", "ocsp", "mitm", "governance", "deterrence", "attribution",
        "dynamic programming", "greedy heuristic", "data preparation", "experimental evaluation",
    ]

    for kw in competency_keywords:
        if kw in blob:
            competency_tags.append(kw.replace(" ", "_"))

    known_tools = [
        "ftk imager", "autopsy", "redline", "magnet", "regripper", "hxd",
        "bulk extractor", "john the ripper", "pdfcrack", "veracrypt",
        "nmap", "tcpdump", "wireshark", "gns3", "cisco asav", "radius",
        "syslog", "ntp", "bgp", "burp suite", "owasp juice shop",
        "openssl", "apache", "curl", "tc netem", "mitmproxy",
        "python", "sql", "pandas",
    ]

    for tool in known_tools:
        if tool in blob:
            tool_tags.append(tool)

    project_keywords = [
        "pki", "ocsp", "trusted-ca mitm", "logistics optimization",
        "container loading", "cyber war", "cyb 260", "cyb 300", "cyb 320",
        "lockbit", "dirty pipe", "cig-amf",
    ]

    for kw in project_keywords:
        if kw in blob:
            project_tags.append(kw.replace(" ", "_"))

    return (
        sorted(set(role_families)),
        sorted(set(competency_tags)),
        sorted(set(tool_tags)),
        sorted(set(project_tags)),
    )


def overclaim_rules(asset_type: str) -> List[str]:
    base = [
        "Do not convert coursework or lab exposure into employment experience.",
        "Do not use 'expert' or 'professional experience' unless source evidence explicitly supports it.",
        "Preserve project scope, methodology, tools, results, and limitations instead of collapsing into a generic skill sentence.",
        "Prefer workflow/capability phrasing over isolated tool-list phrasing.",
    ]

    if asset_type in {"tool_workflow_asset", "project_asset", "research_project_asset"}:
        base.append("When naming tools, connect them to workflow and source context.")
    if asset_type == "strategic_course_asset":
        base.append("Do not treat strategic/policy coursework as hands-on technical tooling.")
    if asset_type == "research_project_asset":
        base.append("Separate proposed research design from completed experimental results.")

    return base


def build_canonical_narrative(title: str, file_name: str, asset_type: str, chosen_sections: List[Tuple[str, str]]) -> str:
    parts = [
        f"ASSET TITLE: {title}",
        f"SOURCE FILE: {file_name}",
        f"ASSET TYPE: {asset_type}",
        "",
        "SOURCE-PRESERVING CANONICAL NARRATIVE:",
        "This asset preserves the source document's higher-level structure. It should not be reduced to tiny skill atoms.",
        "",
    ]

    for section_title, body in chosen_sections:
        excerpt = body.strip()
        if len(excerpt) > 3500:
            excerpt = excerpt[:3500].rstrip() + "\n[TRUNCATED_SECTION_FOR_ASSET_CANONICAL_NARRATIVE]"
        parts.append(f"## {section_title}")
        parts.append(excerpt)
        parts.append("")

    return "\n".join(parts).strip()


def fetch_files(cur, args):
    where = [
        "source = 'local_profile_ingestion'",
        "is_active = true",
        "parsed_text_path IS NOT NULL",
    ]
    params = []

    if args.file_like:
        likes = [f"%{x}%" for x in args.file_like]
        where.append("file_name ILIKE ANY(%s)")
        params.append(likes)

    if args.source_role:
        where.append("file_role = ANY(%s)")
        params.append(args.source_role)

    params.append(args.limit)

    cur.execute(
        f"""
        SELECT
          id,
          file_name,
          file_role,
          evidence_weight,
          parsed_text_path,
          original_local_path
        FROM raw_files
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE
            WHEN file_name ILIKE '%%Tools%%' THEN 1
            WHEN file_name ILIKE '%%TỔNG%%' OR file_name ILIKE '%%TỔNG%%' THEN 2
            WHEN file_name ILIKE '%%Project%%' THEN 3
            WHEN file_name ILIKE '%%Strategic%%' THEN 4
            ELSE 5
          END,
          file_name
        LIMIT %s;
        """,
        params,
    )

    return cur.fetchall()


def insert_component_run(cur, raw_file_id, input_json, output_json):
    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,
          task_type,
          source_file_id,
          input_json,
          output_json,
          output_text,
          status,
          model_provider,
          model_name,
          input_tokens,
          output_tokens,
          estimated_cost_usd,
          finished_at
        )
        VALUES (
          %s,
          %s,
          %s,
          %s,
          %s,
          %s,
          'completed',
          'deterministic_source_preserving',
          %s,
          0,
          0,
          0,
          now()
        )
        RETURNING id;
        """,
        (
            COMPONENT_NAME,
            TASK_TYPE,
            raw_file_id,
            Jsonb(input_json),
            Jsonb(output_json),
            json.dumps(output_json, ensure_ascii=False),
            COMPILER_VERSION,
        ),
    )
    return cur.fetchone()[0]


def compile_file(cur, row, max_sections: int):
    raw_file_id, file_name, file_role, evidence_weight, parsed_text_path, original_local_path = row

    path = Path(parsed_text_path)
    if not path.exists():
        raise RuntimeError(f"parsed_text_path does not exist: {parsed_text_path}")

    text = clean_text(path.read_text(errors="ignore"))
    title = first_title(text, file_name)
    sections = split_sections(text)

    scored = sorted(
        sections,
        key=lambda item: section_score(item[0], item[1]),
        reverse=True,
    )

    chosen = scored[:max_sections]
    chosen = sorted(chosen, key=lambda item: text.find(item[0]))

    asset_type = pick_asset_type(file_name, title, text, file_role)
    role_families, competency_tags, tool_tags, project_tags = infer_tags(text)

    canonical_narrative = build_canonical_narrative(title, file_name, asset_type, chosen)

    job_oriented_summary = (
        "Needs review/generation from source-preserving asset. "
        "Do not replace this with a generic skill sentence."
    )

    input_json = {
        "raw_file_id": str(raw_file_id),
        "file_name": file_name,
        "file_role": file_role,
        "parsed_text_path": parsed_text_path,
        "compiler_version": COMPILER_VERSION,
        "max_sections": max_sections,
    }

    output_json = {
        "asset_title": title,
        "asset_type": asset_type,
        "selected_sections": [s[0] for s in chosen],
        "role_families": role_families,
        "competency_tags": competency_tags,
        "tool_tags": tool_tags,
        "project_tags": project_tags,
    }

    insert_component_run(cur, raw_file_id, input_json, output_json)

    cur.execute(
        """
        INSERT INTO profile_assets (
          asset_title,
          asset_type,
          abstraction_level,
          status,
          canonical_narrative,
          job_oriented_summary,
          role_families,
          competency_tags,
          tool_tags,
          project_tags,
          do_not_overclaim_rules,
          created_from_raw_file_id,
          compiler_version,
          source_strategy,
          confidence
        )
        VALUES (
          %s, %s,
          'source_preserving_asset',
          'needs_review',
          %s,
          %s,
          %s, %s, %s, %s, %s,
          %s,
          %s,
          'source_preserving_section_compilation',
          %s
        )
        ON CONFLICT (compiler_version, created_from_raw_file_id, asset_title)
        DO UPDATE SET
          asset_type = EXCLUDED.asset_type,
          abstraction_level = EXCLUDED.abstraction_level,
          status = 'needs_review',
          canonical_narrative = EXCLUDED.canonical_narrative,
          job_oriented_summary = EXCLUDED.job_oriented_summary,
          role_families = EXCLUDED.role_families,
          competency_tags = EXCLUDED.competency_tags,
          tool_tags = EXCLUDED.tool_tags,
          project_tags = EXCLUDED.project_tags,
          do_not_overclaim_rules = EXCLUDED.do_not_overclaim_rules,
          source_strategy = EXCLUDED.source_strategy,
          confidence = EXCLUDED.confidence,
          updated_at = now()
        RETURNING id;
        """,
        (
            title,
            asset_type,
            canonical_narrative,
            job_oriented_summary,
            role_families,
            competency_tags,
            tool_tags,
            project_tags,
            overclaim_rules(asset_type),
            raw_file_id,
            COMPILER_VERSION,
            float(evidence_weight) if evidence_weight is not None else 0.80,
        ),
    )

    asset_id = cur.fetchone()[0]

    cur.execute("DELETE FROM profile_asset_evidence_items WHERE profile_asset_id = %s;", (asset_id,))

    for idx, (section_title, body) in enumerate(chosen, start=1):
        evidence_text = body.strip()
        if len(evidence_text) > 5000:
            evidence_text = evidence_text[:5000].rstrip() + "\n[TRUNCATED_EVIDENCE_ITEM]"

        evidence_type = "source_excerpt"
        low = section_title.lower()
        if "purpose" in low:
            evidence_type = "purpose"
        elif "narrative" in low:
            evidence_type = "narrative"
        elif "methodology" in low or "testbed" in low:
            evidence_type = "methodology"
        elif "result" in low:
            evidence_type = "result"
        elif "positioning" in low:
            evidence_type = "positioning"
        elif "resume phrase" in low:
            evidence_type = "resume_phrase"

        cur.execute(
            """
            INSERT INTO profile_asset_evidence_items (
              profile_asset_id,
              raw_file_id,
              evidence_rank,
              evidence_type,
              section_title,
              evidence_text,
              source_file_name,
              source_path
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                asset_id,
                raw_file_id,
                idx,
                evidence_type,
                section_title,
                evidence_text,
                file_name,
                original_local_path or parsed_text_path,
            ),
        )

    return {
        "asset_id": str(asset_id),
        "asset_title": title,
        "asset_type": asset_type,
        "sections": len(chosen),
        "file_name": file_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-sections", type=int, default=12)
    parser.add_argument("--file-like", action="append", default=[])
    parser.add_argument("--source-role", action="append", default=[])

    args = parser.parse_args()

    print("===== PORTFOLIO ASSET COMPILER =====")
    print(f"Version:      {COMPILER_VERSION}")
    print(f"Limit:        {args.limit}")
    print(f"Max sections: {args.max_sections}")
    print(f"File filters: {args.file_like}")
    print("")

    compiled = []

    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            rows = fetch_files(cur, args)
            print(f"Files selected: {len(rows)}")

            for row in rows:
                print(f"- {row[1]}")
                result = compile_file(cur, row, args.max_sections)
                compiled.append(result)
                print(f"  asset: {result['asset_type']} :: {result['asset_title'][:100]}")

        conn.commit()

    print("")
    print(f"Compiled assets: {len(compiled)}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
