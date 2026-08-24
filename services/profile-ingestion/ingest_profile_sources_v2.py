import argparse
import hashlib
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn

DSN = database_dsn()


def db_value(value):
    if isinstance(value, dict):
        return Jsonb(value)
    return value


SOURCE_ROOT = Path("data/profile_sources_v2")
PARSED_ROOT = Path("data/profile_parsed_v2")
SOURCE_NAME = "profile_sources_v2"
VERSION = "profile_sources_v2_ingestor_2026_04_27"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def table_columns(cur, table_name: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (table_name,),
    )
    return {r[0] for r in cur.fetchall()}


def insert_dynamic(cur, table: str, values: Dict[str, object], returning: str = "id"):
    cols = list(values.keys())
    q = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING {ret}").format(
        table=sql.Identifier(table),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        vals=sql.SQL(", ").join(sql.Placeholder() for _ in cols),
        ret=sql.Identifier(returning),
    )
    cur.execute(q, [db_value(values[c]) for c in cols])
    return cur.fetchone()[0]


def update_dynamic(cur, table: str, key_col: str, key_val, values: Dict[str, object]):
    if not values:
        return
    cols = list(values.keys())
    q = sql.SQL("UPDATE {table} SET {sets} WHERE {key_col} = %s").format(
        table=sql.Identifier(table),
        sets=sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder())
            for c in cols
        ),
        key_col=sql.Identifier(key_col),
    )
    cur.execute(q, [db_value(values[c]) for c in cols] + [key_val])


def infer_source_role(rel_path: Path) -> Tuple[str, float, bool, bool, str, str, bool]:
    parts = rel_path.parts
    top = parts[0] if parts else ""
    name = rel_path.name.lower()

    if top == "00_official":
        if "resume" in name:
            return ("primary_profile_evidence", 0.95, True, True, "official_resume", "Official external-facing resume.", True)
        if "transcript" in name:
            return ("primary_profile_evidence", 0.95, True, True, "official_transcript", "Official transcript / academic record.", True)
        return ("primary_profile_evidence", 0.90, True, True, "official_document", "Official profile evidence.", True)

    if top == "01_course_profiles":
        return ("enriched_profile_evidence", 0.85, True, True, "course_profile", "Synthesized course profile about user's academic evidence.", True)

    if top == "02_project_profiles":
        if "research_profile" in name or "research_paper" in name or "cig_amf" in name:
            return ("project_artifact_evidence", 0.82, True, True, "research_profile", "Research/project profile; must distinguish proposed work from completed results.", True)
        return ("project_artifact_evidence", 0.85, True, True, "project_profile", "Synthesized project profile based on user's project.", True)

    if top == "03_cross_portfolio_mappings":
        return ("enriched_profile_evidence", 0.80, True, True, "cross_portfolio_mapping", "Cross-portfolio mapping/narrative; useful for synthesis but must preserve source boundaries.", True)

    if top == "04_source_papers_and_course_readings":
        return ("course_reference_material", 0.40, False, True, "source_paper", "Original paper/course reading; background/reference, not direct proof of user's work.", False)

    if top == "05_guidance_not_truth":
        return ("career_strategy_guidance", 0.30, False, False, "guidance_not_truth", "Guidance/planning source; not profile truth.", False)

    if top == "99_source_bundles":
        return ("source_bundle_cross_reference", 0.55, False, True, "source_bundle", "Cross-reference bundle; lower priority than individual source files.", False)

    return ("unclassified", 0.50, False, False, "unknown", "Unclassified source.", False)


def title_from_path(rel_path: Path) -> str:
    stem = rel_path.stem
    stem = re.sub(r"__v\d+$", "", stem)
    return stem.replace("__", " / ").replace("_", " ")


def find_parsed_path(rel_path: Path) -> Optional[Path]:
    p = (PARSED_ROOT / rel_path).with_suffix(".txt")
    return p if p.exists() else None


def split_sections(text: str) -> List[Tuple[str, str]]:
    text = text.replace("\x0c", "\n")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    lines = text.splitlines()
    sections: List[Tuple[str, List[str]]] = []
    title = "Document Opening"
    buf: List[str] = []

    heading_re = re.compile(
        r"^("
        r"\-{3,}\s*PAGE\s+\d+\s*\-{3,}|"
        r"\d+(\.\d+)*[\.\)]\s+.{3,140}|"
        r"[IVX]+\.\s+.{3,140}|"
        r"[A-ZÀ-Ỹ][A-ZÀ-Ỹ0-9/&,\-\s]{6,140}"
        r")$"
    )

    for line in lines:
        s = line.strip()
        is_heading = bool(s and len(s) <= 160 and heading_re.match(s))
        if is_heading:
            if "\n".join(buf).strip():
                sections.append((title, buf))
            title = s
            buf = []
        else:
            buf.append(line)

    if "\n".join(buf).strip():
        sections.append((title, buf))

    out = []
    for t, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if len(body) >= 40:
            out.append((t[:500], body))

    return out or [("Document Opening", text)]


def infer_section_type(title: str, body: str) -> str:
    blob = f"{title}\n{body[:800]}".lower()

    if "research question" in blob:
        return "research_question"
    if any(x in blob for x in ["methodology", "testbed", "experiment", "implementation"]):
        return "methodology"
    if any(x in blob for x in ["result", "finding", "evaluation", "experimental results"]):
        return "result"
    if any(x in blob for x in ["scope", "positioning", "big picture", "why this"]):
        return "scope"
    if any(x in blob for x in ["tool", "framework", "nmap", "burp", "openssl", "autopsy", "ftk"]):
        return "tool_workflow"
    if any(x in blob for x in ["resume phrase", "portfolio positioning"]):
        return "career_positioning"
    if any(x in blob for x in ["limitation", "do not", "overclaim", "should not"]):
        return "limitation"
    return "source_section"


def infer_chunk_category(title: str, body: str) -> str:
    blob = f"{title}\n{body[:800]}".lower()
    if any(x in blob for x in ["gpa", "transcript", "coursework", "rider university"]):
        return "academic"
    if any(x in blob for x in ["project", "research question", "methodology", "testbed", "implementation"]):
        return "project"
    if any(x in blob for x in ["tool", "framework", "burp", "nmap", "openssl", "sql", "python"]):
        return "skills"
    if any(x in blob for x in ["team lead", "intern", "tutor", "experience"]):
        return "experience"
    if any(x in blob for x in ["research", "causal", "mean field", "reinforcement learning"]):
        return "research"
    return "unknown"


def chunk_text(section_title: str, section_body: str, max_words: int, overlap_words: int):
    words = section_body.split()
    if len(words) <= max_words:
        yield section_title, section_body
        return

    start = 0
    idx = 1
    while start < len(words):
        end = min(start + max_words, len(words))
        yield f"{section_title} chunk {idx}", " ".join(words[start:end])
        if end >= len(words):
            break
        start = max(0, end - overlap_words)
        idx += 1


def choose_chunk_columns(cols: set[str]) -> Tuple[str, str]:
    file_col = next((c for c in ["raw_file_id", "source_file_id", "source_file", "file_id"] if c in cols), None)
    text_col = next((c for c in ["chunk_text", "content", "text", "source_text", "text_content"] if c in cols), None)
    if not file_col or not text_col:
        raise RuntimeError(f"Cannot infer profile_chunks columns. Found: {sorted(cols)}")
    return file_col, text_col


def existing_raw_id(cur, cols: set[str], digest: str, original_path: str):
    predicates = []
    params = []

    if "sha256" in cols:
        predicates.append("sha256 = %s")
        params.append(digest)
    if "original_local_path" in cols:
        predicates.append("original_local_path = %s")
        params.append(original_path)

    if not predicates:
        return None

    cur.execute(f"SELECT id FROM raw_files WHERE {' OR '.join(predicates)} LIMIT 1", params)
    row = cur.fetchone()
    return row[0] if row else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--source-root", default=str(SOURCE_ROOT))
    ap.add_argument("--parsed-root", default=str(PARSED_ROOT))
    ap.add_argument("--max-words", type=int, default=720)
    ap.add_argument("--overlap-words", type=int, default=80)
    ap.add_argument("--force-reingest", action="store_true", help="Rebuild unchanged source rows/chunks and reset mapping intentionally.")
    args = ap.parse_args()

    source_root = Path(args.source_root)
    parsed_root = Path(args.parsed_root)

    files = sorted(
        p for p in source_root.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )

    print("===== PROFILE SOURCES V2 INGESTOR =====")
    print(f"Version:     {VERSION}")
    print(f"Mode:        {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Source root: {source_root}")
    print(f"Parsed root: {parsed_root}")
    print(f"Files:       {len(files)}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            raw_cols = table_columns(cur, "raw_files")
            chunk_cols = table_columns(cur, "profile_chunks")
            doc_cols = table_columns(cur, "profile_documents")
            section_cols = table_columns(cur, "profile_document_sections")

            chunk_file_col, chunk_text_col = choose_chunk_columns(chunk_cols)

            print("===== DB SHAPE =====")
            print(f"profile_chunks file column: {chunk_file_col}")
            print(f"profile_chunks text column: {chunk_text_col}")
            print("")

            total_chunks = 0
            total_sections = 0
            missing_parsed = []
            inserted_raw = 0
            updated_raw = 0
            inserted_chunks = 0
            inserted_sections = 0
            upserted_docs = 0

            for source_path in files:
                rel = source_path.relative_to(source_root)
                parsed_path = (parsed_root / rel).with_suffix(".txt")
                parsed_path = parsed_path if parsed_path.exists() else None
                role, weight, allow_promo, allow_retrieval, doc_type, notes, contains_evidence = infer_source_role(rel)

                if not parsed_path:
                    missing_parsed.append(str(rel))
                    parsed_text = ""
                    sections = []
                    parse_status = "missing_parsed_text"
                else:
                    parsed_text = parsed_path.read_text(errors="ignore")
                    sections = split_sections(parsed_text)
                    parse_status = "parsed"

                chunk_plan = []
                for sec_idx, (sec_title, sec_body) in enumerate(sections, start=1):
                    for chunk_title, body in chunk_text(sec_title, sec_body, args.max_words, args.overlap_words):
                        token_count = len(body.split())
                        if token_count >= 5:
                            chunk_plan.append((sec_idx, sec_title, chunk_title, body, token_count))

                total_sections += len(sections)
                total_chunks += len(chunk_plan)

                print(f"- {rel}")
                print(f"  role:       {role}")
                print(f"  doc_type:   {doc_type}")
                print(f"  parsed:     {parsed_path.relative_to(parsed_root) if parsed_path else 'MISSING'}")
                print(f"  sections:   {len(sections)}")
                print(f"  chunks:     {len(chunk_plan)}")

                if not args.apply:
                    continue

                digest = sha256_file(source_path)
                mime_type = mimetypes.guess_type(source_path.name)[0]
                file_type = source_path.suffix.lstrip(".").lower() or "unknown"
                original_path = str(source_path.resolve())

                raw_values = {
                    "file_name": source_path.name,
                    "file_type": file_type,
                    "mime_type": mime_type,
                    "storage_url": f"local://{original_path}",
                    "sha256": digest,
                    "source": SOURCE_NAME,
                    "parse_status": parse_status,
                    "parser_used": "existing_profile_parsed_v2",
                    "is_active": True,
                    "file_role": role,
                    "evidence_weight": weight,
                    "allow_profile_fact_promotion": allow_promo,
                    "allow_profile_pack_retrieval": allow_retrieval,
                    "file_role_notes": notes,
                    "original_local_path": original_path,
                    "parsed_text_path": str(parsed_path.resolve()) if parsed_path else None,
                    "file_size_bytes": source_path.stat().st_size,
                    "source_path_verified": True,
                }
                raw_values = {k: v for k, v in raw_values.items() if k in raw_cols}

                raw_id = existing_raw_id(cur, raw_cols, digest, original_path)
                if raw_id and not args.force_reingest:
                    cur.execute(
                        """SELECT rf.sha256, rf.original_local_path, pd.id
                             FROM raw_files rf
                             LEFT JOIN profile_documents pd
                               ON pd.raw_file_id = rf.id AND pd.document_title = %s
                            WHERE rf.id = %s
                            LIMIT 1""",
                        (title_from_path(rel), raw_id),
                    )
                    existing = cur.fetchone()
                    if (existing and str(existing[0] or "") == digest
                            and str(existing[1] or "") == original_path and existing[2]):
                        print("  unchanged: preserving existing document mapping/evidence state")
                        continue

                if raw_id:
                    update_values = dict(raw_values)
                    update_values.pop("sha256", None)
                    update_dynamic(cur, "raw_files", "id", raw_id, update_values)
                    updated_raw += 1
                else:
                    raw_id = insert_dynamic(cur, "raw_files", raw_values)
                    inserted_raw += 1

                # Rebuild chunks for this source.
                cur.execute(
                    sql.SQL("DELETE FROM profile_chunks WHERE {} = %s").format(sql.Identifier(chunk_file_col)),
                    (raw_id,),
                )

                # Upsert profile_document.
                document_title = title_from_path(rel)
                cur.execute(
                    """
                    SELECT id FROM profile_documents
                    WHERE raw_file_id = %s AND document_title = %s
                    LIMIT 1
                    """,
                    (raw_id, document_title),
                )
                row = cur.fetchone()

                doc_values = {
                    "raw_file_id": raw_id,
                    "document_title": document_title,
                    "document_type": doc_type,
                    "document_purpose": notes,
                    "source_role": role,
                    "source_quality": weight,
                    "contains_profile_evidence": contains_evidence,
                    "contains_guidance_only": role == "career_strategy_guidance",
                    "parser_used": "existing_profile_parsed_v2",
                    "parsed_text_path": str(parsed_path.resolve()) if parsed_path else None,
                    "original_local_path": original_path,
                    "document_summary": None,
                    "structure_json": {"source_relative_path": str(rel), "section_count": len(sections)},
                    "risk_notes": [],
                    "mapper_version": None,
                    "mapper_model": None,
                    "status": "needs_mapping",
                }
                doc_values = {k: v for k, v in doc_values.items() if k in doc_cols}

                if row:
                    doc_id = row[0]
                    update_values = dict(doc_values)
                    update_values.pop("raw_file_id", None)
                    update_values.pop("document_title", None)
                    update_dynamic(cur, "profile_documents", "id", doc_id, update_values)
                else:
                    doc_id = insert_dynamic(cur, "profile_documents", doc_values)

                upserted_docs += 1

                cur.execute(
                    "DELETE FROM profile_document_sections WHERE profile_document_id = %s",
                    (doc_id,),
                )

                chunk_index = 0
                section_index = 0

                for sec_idx, sec_title, chunk_title, body, token_count in chunk_plan:
                    chunk_index += 1
                    section_index += 1

                    chunk_values = {
                        chunk_file_col: raw_id,
                        "chunk_index": chunk_index,
                        "section": chunk_title[:500],
                        "category": infer_chunk_category(chunk_title, body),
                        chunk_text_col: body,
                        "token_count": token_count,
                        "source": SOURCE_NAME,
                    }
                    chunk_values = {k: v for k, v in chunk_values.items() if k in chunk_cols}
                    chunk_id = insert_dynamic(cur, "profile_chunks", chunk_values)
                    inserted_chunks += 1

                    section_values = {
                        "profile_document_id": doc_id,
                        "raw_file_id": raw_id,
                        "chunk_id": chunk_id,
                        "section_index": section_index,
                        "section_title": chunk_title[:500],
                        "section_type": infer_section_type(chunk_title, body),
                        "section_text": body,
                        "section_summary": None,
                        "importance_score": 0.50,
                        "model_notes": None,
                    }
                    section_values = {k: v for k, v in section_values.items() if k in section_cols}
                    insert_dynamic(cur, "profile_document_sections", section_values)
                    inserted_sections += 1

            if args.apply:
                conn.commit()

    print("")
    print("===== SUMMARY =====")
    print(f"Files planned:      {len(files)}")
    print(f"Missing parsed:     {len(missing_parsed)}")
    print(f"Sections planned:   {total_sections}")
    print(f"Chunks planned:     {total_chunks}")

    if args.apply:
        print(f"Raw inserted:       {inserted_raw}")
        print(f"Raw updated:        {updated_raw}")
        print(f"Documents upserted: {upserted_docs}")
        print(f"Chunks inserted:    {inserted_chunks}")
        print(f"Sections inserted:  {inserted_sections}")
    else:
        print("Dry-run only. Re-run with --apply to write DB.")

    if missing_parsed:
        print("")
        print("===== MISSING PARSED FILES =====")
        for x in missing_parsed:
            print(f"- {x}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
