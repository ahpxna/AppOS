#!/usr/bin/env python3
"""Version DOCX/PDF/text profile sources by content SHA and record provenance timestamps.

Created/modified timestamps are retained as evidence only.  They never decide
whether a profile document changed; ``sha256`` is the revision identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "data/profile_sources_v2"
SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt", ".md"}
TRACKER_VERSION = "profile_source_revisions_v1_2026_08_24"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_pdf_date(value: Any) -> datetime | None:
    """Parse the common PDF ``D:YYYYMMDDHHmmSSOHH'mm'`` form conservatively."""
    if not value:
        return None
    raw = str(value).strip()
    if raw.startswith("D:"):
        raw = raw[2:]
    match = re.match(
        r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz]|[+-]\d{2}'?\d{2}'?)?$",
        raw,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = [int(x) if x else None for x in match.groups()[:6]]
    tz_raw = match.group(7)
    try:
        tz = timezone.utc
        if tz_raw and tz_raw.upper() != "Z":
            sign = 1 if tz_raw[0] == "+" else -1
            digits = re.sub(r"\D", "", tz_raw[1:])
            if len(digits) >= 4:
                offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
                tz = timezone(sign * offset)
        return datetime(year, month or 1, day or 1, hour or 0, minute or 0, second or 0, tzinfo=tz)
    except ValueError:
        return None


def embedded_metadata(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    payload: dict[str, Any] = {"source_kind": suffix.lstrip(".") or "other"}
    if suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        props = document.core_properties
        payload.update({
            "embedded_created_at": _iso_or_none(props.created),
            "embedded_modified_at": _iso_or_none(props.modified),
            "embedded_title": props.title or None,
            "embedded_subject": props.subject or None,
            "embedded_author": props.author or None,
            "embedded_last_modified_by": props.last_modified_by or None,
            "embedded_revision": props.revision,
        })
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        meta = reader.metadata or {}
        payload.update({
            "embedded_created_at": _iso_or_none(_parse_pdf_date(meta.get("/CreationDate"))),
            "embedded_modified_at": _iso_or_none(_parse_pdf_date(meta.get("/ModDate"))),
            "embedded_title": str(meta.get("/Title") or "") or None,
            "embedded_author": str(meta.get("/Author") or "") or None,
            "pdf_page_count": len(reader.pages),
        })
    else:
        payload.update({"embedded_created_at": None, "embedded_modified_at": None})
    return payload


def authority_class_for_relative_path(relative_path: Path) -> str:
    top = relative_path.parts[0] if relative_path.parts else ""
    if top == "00_official":
        return "official_document"
    if top == "02_project_profiles":
        return "project_document"
    if top == "04_source_papers_and_course_readings":
        return "reference_document"
    if top == "05_guidance_not_truth":
        return "guidance_document"
    if top in {"01_course_profiles", "03_cross_portfolio_mappings"}:
        return "profile_document"
    return "unknown"


def source_metadata(path: Path, source_root: Path = SOURCE_ROOT) -> dict[str, Any]:
    resolved = path.resolve()
    stat = resolved.stat()
    try:
        rel = resolved.relative_to(source_root.resolve())
    except ValueError:
        rel = Path(resolved.name)
    embedded = embedded_metadata(resolved)
    birth = getattr(stat, "st_birthtime", None)
    filesystem_birth = datetime.fromtimestamp(birth, tz=timezone.utc) if birth is not None else None
    filesystem_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return {
        "logical_source_key": f"profile_sources_v2/{rel.as_posix()}",
        "display_name": resolved.name,
        "relative_path": rel.as_posix(),
        "source_kind": embedded.get("source_kind") or resolved.suffix.lower().lstrip(".") or "other",
        "authority_class": authority_class_for_relative_path(rel),
        "content_sha256": sha256_file(resolved),
        "embedded_created_at": embedded.get("embedded_created_at"),
        "embedded_modified_at": embedded.get("embedded_modified_at"),
        "filesystem_birth_at": _iso_or_none(filesystem_birth),
        "filesystem_modified_at": _iso_or_none(filesystem_modified),
        "metadata": {
            **embedded,
            "filesystem_ctime": _iso_or_none(datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)),
            "file_size_bytes": stat.st_size,
            "tracker_version": TRACKER_VERSION,
        },
    }


def discover_sources(source_root: Path = SOURCE_ROOT) -> list[Path]:
    if not source_root.exists():
        return []
    return sorted(
        p for p in source_root.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and ":Zone.Identifier" not in p.name
        and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _load_db():
    import psycopg
    from psycopg.types.json import Jsonb

    sys.path.insert(0, str(ROOT))
    from services.common.config import database_dsn
    return psycopg, Jsonb, database_dsn


def sync_revisions(*, source_root: Path = SOURCE_ROOT, apply: bool, prune_missing: bool = True) -> dict[str, Any]:
    psycopg, Jsonb, database_dsn = _load_db()
    files = discover_sources(source_root)
    seen_keys: set[str] = set()
    stats = {"sources": len(files), "new_documents": 0, "new_revisions": 0, "unchanged": 0, "missing": 0}
    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        for path in files:
            item = source_metadata(path, source_root)
            key = item["logical_source_key"]
            seen_keys.add(key)
            cur.execute(
                """
                INSERT INTO profile_source_documents
                  (logical_source_key, display_name, source_kind, authority_class, status, last_seen_at, updated_at)
                VALUES (%s,%s,%s,%s,'active',now(),now())
                ON CONFLICT (logical_source_key)
                DO UPDATE SET display_name=EXCLUDED.display_name,
                              source_kind=EXCLUDED.source_kind,
                              authority_class=EXCLUDED.authority_class,
                              status='active', last_seen_at=now(), updated_at=now()
                RETURNING id::text, current_revision_id::text;
                """,
                (key, item["display_name"], item["source_kind"], item["authority_class"]),
            )
            source_document_id, previous_revision_id = cur.fetchone()
            cur.execute(
                """
                SELECT id::text FROM profile_source_revisions
                WHERE source_document_id=%s AND content_sha256=%s;
                """,
                (source_document_id, item["content_sha256"]),
            )
            row = cur.fetchone()
            if row:
                revision_id = row[0]
                stats["unchanged"] += 1
                cur.execute(
                    """
                    UPDATE profile_source_revisions
                    SET status='current', ingested_at=now(), metadata_json=%s,
                        embedded_created_at=%s, embedded_modified_at=%s,
                        filesystem_birth_at=%s, filesystem_modified_at=%s
                    WHERE id=%s;
                    """,
                    (Jsonb(item["metadata"]), item["embedded_created_at"], item["embedded_modified_at"],
                     item["filesystem_birth_at"], item["filesystem_modified_at"], revision_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO profile_source_revisions
                      (source_document_id, content_sha256, embedded_created_at, embedded_modified_at,
                       filesystem_birth_at, filesystem_modified_at, parser_fingerprint, metadata_json, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'current')
                    RETURNING id::text;
                    """,
                    (source_document_id, item["content_sha256"], item["embedded_created_at"],
                     item["embedded_modified_at"], item["filesystem_birth_at"], item["filesystem_modified_at"],
                     TRACKER_VERSION, Jsonb(item["metadata"])),
                )
                revision_id = cur.fetchone()[0]
                stats["new_revisions"] += 1
                if previous_revision_id is None:
                    stats["new_documents"] += 1
            cur.execute(
                """
                UPDATE profile_source_revisions
                SET status='superseded'
                WHERE source_document_id=%s AND id<>%s AND status='current';
                """,
                (source_document_id, revision_id),
            )
            cur.execute(
                "UPDATE profile_source_documents SET current_revision_id=%s WHERE id=%s",
                (revision_id, source_document_id),
            )
            # Bind to the exact current raw/profile-document row once the V2
            # ingestor has created it.  Path + SHA is intentional: timestamps
            # are provenance only and two revisions at one path must never be
            # confused merely because Office retained the same Created value.
            original_path = str(path.resolve())
            cur.execute(
                """
                SELECT id::text FROM raw_files
                WHERE source='profile_sources_v2' AND sha256=%s
                  AND original_local_path=%s AND is_active=true
                LIMIT 1;
                """,
                (item["content_sha256"], original_path),
            )
            raw_row = cur.fetchone()
            if raw_row:
                raw_id = raw_row[0]
                cur.execute(
                    "UPDATE profile_source_revisions SET raw_file_id=%s WHERE id=%s",
                    (raw_id, revision_id),
                )
                cur.execute(
                    "UPDATE raw_files SET source_revision_id=%s WHERE id=%s",
                    (revision_id, raw_id),
                )
                cur.execute(
                    """
                    UPDATE profile_documents
                    SET source_revision_id=%s, updated_at=now()
                    WHERE raw_file_id=%s;
                    """,
                    (revision_id, raw_id),
                )
        if prune_missing:
            cur.execute("SELECT id::text, logical_source_key, current_revision_id::text FROM profile_source_documents WHERE status='active'")
            for source_id, key, revision_id in cur.fetchall():
                if key.startswith("profile_sources_v2/") and key not in seen_keys:
                    stats["missing"] += 1
                    cur.execute(
                        "UPDATE profile_source_documents SET status='missing', updated_at=now() WHERE id=%s",
                        (source_id,),
                    )
                    if revision_id:
                        cur.execute("UPDATE profile_source_revisions SET status='missing' WHERE id=%s", (revision_id,))
        if apply:
            conn.commit()
        else:
            conn.rollback()
    stats["committed"] = apply
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Version profile source files by immutable content SHA.")
    parser.add_argument("command", choices=("scan", "sync"))
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    args = parser.parse_args()
    if args.command == "scan":
        print(json.dumps([source_metadata(path, args.source_root) for path in discover_sources(args.source_root)], indent=2))
        return 0
    print(json.dumps(sync_revisions(source_root=args.source_root, apply=args.apply, prune_missing=not args.no_prune), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
