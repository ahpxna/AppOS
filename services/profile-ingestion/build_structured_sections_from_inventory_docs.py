import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn  # noqa: E402


DSN = database_dsn()

# PATCH H4: VERSION tu sinh theo hash cua logic. Doi logic -> doi version
# -> existing_structured_count() khong chan nua -> tu chay lai.
def _logic_version() -> str:
    import hashlib
    import inspect
    src = inspect.getsource(detect_heading) + inspect.getsource(looks_structured)
    return "structured_section_boundary_v2_" + hashlib.sha1(src.encode()).hexdigest()[:10]


VERSION = "PLACEHOLDER_SET_AT_BOTTOM"


NUMBERED_SUBSECTION_RE = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)+)\s+\.?\s*(?P<title>.{2,160})\s*$"
)

NUMBERED_CATEGORY_RE = re.compile(
    r"^\s*(?P<num>\d+)\.\s+(?P<title>.{4,180})\s*$"
)

MARKDOWN_HEADING_RE = re.compile(
    r"^\s*(?P<md>[#]{1,5})\s+(?P<title>.{2,180})\s*$"
)

PLAIN_MAJOR_HEADING_RE = re.compile(
    r"^\s*(?P<title>[A-Z][A-Z0-9+/#&().,'’:;\- ]{4,180})\s*$"
)

FIELD_LABELS = {
    "what it does",
    "what it does:",
    "source",
    "source:",
    "sources",
    "sources:",
    "course",
    "course:",
    "lab",
    "lab:",
    "labs",
    "labs:",
    "portfolio positioning",
    "portfolio positioning:",
    "resume phrase",
    "resume phrase:",
    "resume-safe phrase",
    "resume-safe phrase:",
    "resume-safe phrasing",
    "resume-safe phrasing:",
    "stronger honest phrasing",
    "stronger honest phrasing:",
    "tools/platforms",
    "tools/platforms:",
    "tools/platforms/frameworks",
    "tools/platforms/frameworks:",
    "tools/concepts",
    "tools/concepts:",
    "tools/frameworks",
    "tools/frameworks:",
    "tools & platforms",
    "tools & platforms:",
    "need-to-learn / job-alignment targets",
    "need-to-learn / job-alignment targets:",
}

STRUCTURED_SIGNALS = [
    "source:",
    "sources:",
    "course:",
    "lab ",
    "labs:",
    "resume",
    "portfolio positioning",
    "positioning",
    "role relevance",
    "must not",
    "do not claim",
    "used to",
    "workflow",
    "tool",
    "framework",
    "platform",
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def slugify(s: str) -> str:
    s = norm(s).lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:80] or "section"


def detect_heading(line: str) -> Optional[str]:
    s = norm(line)
    if not s or len(s) > 220:
        return None

    lowered = s.lower().strip().rstrip()
    if lowered in FIELD_LABELS:
        return None

    # Strong section signal: numbered subsection, e.g. "4.1 FTK Imager".
    m = NUMBERED_SUBSECTION_RE.match(s)
    if m:
        title = m.group("title").strip()
        if title.lower() not in FIELD_LABELS and len(title.split()) <= 18:
            return s

    # Major category, e.g. "4. Digital Forensics and Incident Response Tools".
    m = NUMBERED_CATEGORY_RE.match(s)
    if m:
        title = m.group("title").strip()
        low = title.lower()
        if low not in FIELD_LABELS and len(title.split()) <= 20:
            category_signals = [
                "tool",
                "tools",
                "framework",
                "frameworks",
                "security",
                "forensics",
                "network",
                "web application",
                "governance",
                "cryptography",
                "job description",
                "market",
                "resume",
                "interview",
                "positioning",
            ]
            if any(x in low for x in category_signals):
                return s

    # Markdown heading.
    m = MARKDOWN_HEADING_RE.match(s)
    if m:
        title = m.group("title").strip()
        if title.lower() not in FIELD_LABELS and len(title.split()) <= 18:
            return s

    # Plain all-caps document/category title only.
    # This intentionally does NOT match title-case lines such as "Portfolio positioning:".
    m = PLAIN_MAJOR_HEADING_RE.match(s)
    if m:
        title = m.group("title").strip()
        low = title.lower()
        if low in FIELD_LABELS:
            return None
        if len(title.split()) <= 16:
            return s

    return None



def looks_structured(title: str, body: str) -> bool:
    blob = f"{title}\n{body[:2500]}".lower()
    signal_count = sum(1 for x in STRUCTURED_SIGNALS if x in blob)

    # Numbered tool sections like 4.1 FTK Imager should pass even with fewer signals.
    numbered_tool_like = bool(re.match(r"^\s*\d+(?:\.\d+)+\s+", title)) and len(body) >= 120

    return signal_count >= 2 or numbered_tool_like


def infer_section_kind(title: str, body: str) -> str:
    blob = f"{title}\n{body[:1200]}".lower()

    if any(x in blob for x in ["ftk", "autopsy", "redline", "magnet", "forensic", "imager"]):
        return "structured_tool_section"
    if any(x in blob for x in ["nmap", "wireshark", "tcpdump", "network", "packet"]):
        return "structured_tool_section"
    if any(x in blob for x in ["burp", "owasp", "web", "juice shop", "zap"]):
        return "structured_tool_section"
    if any(x in blob for x in ["nist", "iso", "framework", "control", "governance"]):
        return "structured_framework_section"
    if any(x in blob for x in ["source:", "resume", "role relevance", "portfolio positioning"]):
        return "structured_inventory_section"

    return "structured_section"


def extract_boundaries(title: str, body: str, file_name: str) -> Dict:
    text = f"{title}\n{body}"

    courses = sorted(set(re.findall(r"\b(?:CSC|CYB|CIS)\s*[_-]?\s*\d{3}\b", text, flags=re.I)))
    labs = sorted(set(re.findall(r"\bLab\s*\d+\b", text, flags=re.I)))

    tool_name = title
    tool_name = re.sub(r"^\s*\d+(?:\.\d+)+\s+", "", tool_name).strip()
    tool_name = re.sub(r"^#+\s+", "", tool_name).strip()

    return {
        "document": file_name,
        "section": title,
        "tool_or_topic": tool_name,
        "courses": courses,
        "labs": labs,
        "chunking_strategy": "section_boundary",
        "do_not_chunk_mid_unit": True,
    }


def split_structured_sections(text: str) -> List[Tuple[str, str]]:
    text = text.replace("\x0c", "\n")
    text = re.sub(r"\r\n?", "\n", text)
    lines = text.splitlines()

    sections: List[Tuple[str, List[str]]] = []
    current_title = "Document Opening"
    buf: List[str] = []

    for line in lines:
        heading = detect_heading(line)
        if heading:
            body = "\n".join(buf).strip()
            if body:
                sections.append((current_title, buf))
            current_title = heading
            buf = []
        else:
            buf.append(line)

    body = "\n".join(buf).strip()
    if body:
        sections.append((current_title, buf))

    # PATCH M1: dem va in so section bi vut, thay vi bo im lang.
    out: List[Tuple[str, str]] = []
    dropped_short = 0
    dropped_unstructured = 0
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if len(body) < 80:
            dropped_short += 1
            continue
        if not looks_structured(title, body):
            dropped_unstructured += 1
            continue
        out.append((title[:500], body))

    print(
        f"    split: raw={len(sections)} kept={len(out)} "
        f"dropped_short={dropped_short} dropped_unstructured={dropped_unstructured}"
    )
    return out


def fetch_candidate_docs(cur, file_likes: List[str], limit: int):
    params: List[object] = []

    file_like_sql = ""
    if file_likes:
        clauses = []
        for f in file_likes:
            clauses.append("rf.file_name ILIKE %s")
            params.append(f"%{f}%")
        file_like_sql = " OR " + " OR ".join(clauses)

    params.append(limit)

    cur.execute(
        f"""
        SELECT
          pd.id,
          rf.id AS raw_file_id,
          rf.file_name,
          rf.parsed_text_path,
          pd.document_type
        FROM profile_documents pd
        JOIN raw_files rf
          ON rf.id = pd.raw_file_id
        WHERE pd.status = 'mapped'
          AND (
            pd.document_type = 'cross_portfolio_mapping'
            OR rf.file_name ILIKE '%%tool%%'
            OR rf.file_name ILIKE '%%framework%%'
            OR rf.file_name ILIKE '%%mapping%%'
            {file_like_sql}
          )
        ORDER BY rf.file_name
        LIMIT %s
        """,
        params,
    )

    return cur.fetchall()


def existing_structured_count(cur, document_id):
    cur.execute(
        """
        SELECT count(*)
        FROM profile_document_sections
        WHERE profile_document_id = %s
          AND model_notes = %s
        """,
        (document_id, VERSION),
    )
    return cur.fetchone()[0]


def get_max_indexes(cur, raw_file_id, document_id):
    cur.execute(
        "SELECT COALESCE(max(chunk_index), 0) FROM profile_chunks WHERE file_id = %s",
        (raw_file_id,),
    )
    max_chunk = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(max(section_index), 0) FROM profile_document_sections WHERE profile_document_id = %s",
        (document_id,),
    )
    max_section = cur.fetchone()[0]

    return int(max_chunk or 0), int(max_section or 0)


def cleanup_existing(cur, raw_file_id, document_id):
    cur.execute(
        """
        DELETE FROM profile_document_sections
        WHERE profile_document_id = %s
          AND model_notes = %s
        """,
        (document_id, VERSION),
    )

    cur.execute(
        """
        DELETE FROM profile_chunks
        WHERE file_id = %s
          AND metadata->>'structured_rechunk_version' = %s
        """,
        (raw_file_id, VERSION),
    )


def insert_structured_section(cur, doc, section_index: int, chunk_index: int, title: str, body: str):
    document_id, raw_file_id, file_name, parsed_text_path, document_type = doc

    token_count = len(body.split())
    section_kind = infer_section_kind(title, body)
    boundary = extract_boundaries(title, body, file_name)

    chunk_metadata = {
        "structured_rechunk_version": VERSION,
        "chunking_strategy": "section_boundary",
        "document_type": document_type,
        "section_kind": section_kind,
        "source_boundaries": boundary,
    }

    cur.execute(
        """
        INSERT INTO profile_chunks (
          file_id,
          chunk_index,
          section,
          category,
          text_content,
          token_count,
          metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            raw_file_id,
            chunk_index,
            title,
            "structured_inventory",
            body,
            token_count,
            Jsonb(chunk_metadata),
        ),
    )
    chunk_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO profile_document_sections (
          profile_document_id,
          raw_file_id,
          chunk_id,
          section_index,
          section_title,
          section_type,
          section_text,
          section_summary,
          importance_score,
          model_notes,
          structured_section_key,
          structured_section_kind,
          source_boundary_json,
          chunking_strategy
        )
        VALUES (
          %s, %s, %s, %s, %s, %s, %s,
          NULL,
          0.85,
          %s,
          %s,
          %s,
          %s,
          'section_boundary'
        )
        """,
        (
            document_id,
            raw_file_id,
            chunk_id,
            section_index,
            title,
            section_kind,
            body,
            VERSION,
            slugify(title),
            section_kind,
            Jsonb(boundary),
        ),
    )


def update_document_strategy(cur, document_id: str, file_name: str, sections_count: int):
    structure_patch = {
        "structured_rechunk_version": VERSION,
        "document_structure_type": "structured_inventory",
        "chunking_strategy": "section_boundary",
        "do_not_chunk_mid_unit": True,
        "structured_section_count": sections_count,
    }

    cur.execute(
        """
        UPDATE profile_documents
        SET
          document_structure_type = 'structured_inventory',
          chunking_strategy = 'section_boundary',
          do_not_chunk_mid_unit = true,
          structure_confidence = 0.85,
          structure_json = COALESCE(structure_json, '{}'::jsonb) || %s::jsonb,
          updated_at = now()
        WHERE id = %s
        """,
        (Jsonb(structure_patch), document_id),
    )


VERSION = _logic_version()


VERSION = _logic_version()


VERSION = _logic_version()


VERSION = _logic_version()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--file-like", action="append", default=[])
    args = parser.parse_args()

    print("===== STRUCTURED SECTION BOUNDARY BUILDER =====")
    print(f"Version: {VERSION}")
    print(f"Mode:    {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Force:   {args.force}")
    print(f"Limit:   {args.limit}")
    print(f"Filters: {args.file_like}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            docs = fetch_candidate_docs(cur, args.file_like, args.limit)

            print(f"Candidate docs: {len(docs)}")

            total_sections = 0
            changed_docs = 0

            for doc in docs:
                document_id, raw_file_id, file_name, parsed_text_path, document_type = doc

                print("")
                print(f"- {file_name}")
                print(f"  type: {document_type}")

                if not parsed_text_path or not Path(parsed_text_path).exists():
                    print("  SKIP: missing parsed_text_path")
                    continue

                existing = existing_structured_count(cur, document_id)
                if existing and not args.force:
                    print(f"  SKIP: already has {existing} structured sections. Use --force to rebuild.")
                    continue

                text = Path(parsed_text_path).read_text(errors="ignore")
                sections = split_structured_sections(text)

                print(f"  structured sections found: {len(sections)}")

                if not sections:
                    continue

                total_sections += len(sections)
                changed_docs += 1

                if not args.apply:
                    continue

                cur.execute("SAVEPOINT structured_sections_sp")
                try:
                    if args.force:
                        cleanup_existing(cur, raw_file_id, document_id)

                    max_chunk, max_section = get_max_indexes(cur, raw_file_id, document_id)

                    for offset, (title, body) in enumerate(sections, start=1):
                        insert_structured_section(
                            cur,
                            doc,
                            section_index=max_section + offset,
                            chunk_index=max_chunk + offset,
                            title=title,
                            body=body,
                        )

                    update_document_strategy(cur, document_id, file_name, len(sections))
                    cur.execute("RELEASE SAVEPOINT structured_sections_sp")
                    conn.commit()

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT structured_sections_sp")
                    cur.execute("RELEASE SAVEPOINT structured_sections_sp")
                    print(f"  FAILED: {e}")

    print("")
    print("===== SUMMARY =====")
    print(f"Docs with structured sections planned: {changed_docs}")
    print(f"Structured sections planned:          {total_sections}")

    if not args.apply:
        print("Dry-run only. Re-run with --apply.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
