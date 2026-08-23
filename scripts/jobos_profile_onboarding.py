#!/usr/bin/env python3
"""Stage private profile data and review explicitly generated profile assets.

Staging never uploads or calls an LLM.  Asset approval is an explicit, local,
human-reviewed database operation rather than a hidden side effect of ingest.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE_ROOT = ROOT / "data" / "profile_sources_v2"
TEMPLATE_DIR = ROOT / "data" / "resume-template"
TEMPLATE_NAME = "VU PHAN AN NGUYEN-official_For_all.docx"
BUCKETS = {
    "official": "00_official", "course": "01_course_profiles",
    "project": "02_project_profiles", "mapping": "03_cross_portfolio_mappings",
    "reference": "04_source_papers_and_course_readings", "guidance": "05_guidance_not_truth",
}


def validate_template(path: Path) -> list[str]:
    if path.suffix.casefold() != ".docx":
        raise ValueError("Resume template must be a .docx file.")
    try:
        renderer_path = ROOT / "services" / "document-generation" / "resume_template_renderer.py"
        spec = importlib.util.spec_from_file_location("jobos_onboarding_resume_renderer", renderer_path)
        if spec is None or spec.loader is None:
            raise ValueError("Resume renderer is unavailable.")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        result = renderer.validate_template_contract(path)
    except ImportError as exc:
        raise ValueError("Install Python requirements before validating the Word template.") from exc
    except Exception as exc:
        raise ValueError(f"Template does not satisfy the fixed renderer contract: {exc}") from exc
    return [f"{key}={value}" for key, value in sorted(result.items())]


def copy_private(source: Path, destination: Path, *, replace: bool) -> None:
    if not source.is_file():
        raise ValueError(f"File not found: {source}")
    if destination.exists() and not replace:
        raise ValueError(f"Destination already exists: {destination}. Pass --replace only if intentional.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def status() -> dict[str, object]:
    template = TEMPLATE_DIR / TEMPLATE_NAME
    sources = {bucket: len(list((SOURCE_ROOT / directory).glob("*"))) if (SOURCE_ROOT / directory).exists() else 0
               for bucket, directory in BUCKETS.items()}
    return {
        "resume_template": str(template), "template_present": template.is_file(),
        "source_files_by_bucket": sources,
        "project_registry_present": (ROOT / "data" / "project-registry" / "project_profiles.json").is_file(),
        "next": [
            "Review staged sources, then parse_profile_sources_v2.py.",
            "Ingest with ingest_profile_sources_v2.py --apply; assets still require human review/approval.",
            "Open jobos_project_profile_app.py to create the six-project local registry.",
        ],
    }


def review_assets(limit: int) -> list[dict[str, object]]:
    """List the human-review queue without exposing a mutation shortcut."""
    import psycopg
    from services.common.config import database_dsn

    with psycopg.connect(database_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, asset_title, asset_type, status, confidence,
                   coalesce(review_note, ''), left(canonical_narrative, 500)
            FROM profile_assets
            WHERE status IN ('draft', 'needs_review')
            ORDER BY updated_at DESC
            LIMIT %s;
            """,
            (limit,),
        )
        return [
            {"id": row[0], "title": row[1], "type": row[2], "status": row[3],
             "confidence": str(row[4]), "review_note": row[5], "preview": row[6]}
            for row in cur.fetchall()
        ]


def approve_asset(asset_id: str, note: str, *, apply: bool) -> dict[str, object]:
    """Approve exactly one reviewed asset; dry-run remains the default."""
    import psycopg
    from services.common.config import database_dsn

    with psycopg.connect(database_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT asset_title, asset_type, status, left(canonical_narrative, 500)
               FROM profile_assets WHERE id = %s""",
            (asset_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("Profile asset not found.")
        if row[2] not in {"draft", "needs_review"}:
            raise ValueError(f"Only draft/needs_review assets can be approved (current: {row[2]}).")
        result = {"id": asset_id, "title": row[0], "type": row[1], "previous_status": row[2],
                  "preview": row[3], "apply": apply}
        if apply:
            cur.execute(
                """UPDATE profile_assets
                   SET status = 'approved', review_note = %s, updated_at = now()
                   WHERE id = %s""",
                (note.strip() or "Approved manually through JobOS onboarding.", asset_id),
            )
            conn.commit()
            result["status"] = "approved"
        else:
            conn.rollback()
            result["status"] = "dry_run"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage private JobOS profile inputs on a new machine.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    review = sub.add_parser("review", help="List draft/needs_review profile assets for manual inspection.")
    review.add_argument("--limit", type=int, default=20)
    approve = sub.add_parser("approve", help="Approve one reviewed profile asset; dry-run by default.")
    approve.add_argument("asset_id")
    approve.add_argument("--note", default="")
    approve.add_argument("--apply", action="store_true")
    stage = sub.add_parser("stage")
    stage.add_argument("--resume-template", type=Path, help="Your private immutable .docx resume template.")
    stage.add_argument("--source", type=Path, action="append", default=[], help="A resume, transcript, or project evidence file to copy.")
    stage.add_argument("--bucket", choices=sorted(BUCKETS), default="official", help="Destination classification for --source files.")
    stage.add_argument("--replace", action="store_true", help="Allow replacing an existing staged file.")
    stage.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "status":
            print(json.dumps(status(), indent=2))
            return 0
        if args.command == "review":
            if args.limit < 1 or args.limit > 100:
                raise ValueError("--limit must be between 1 and 100.")
            print(json.dumps({"assets": review_assets(args.limit)}, indent=2))
            return 0
        if args.command == "approve":
            print(json.dumps(approve_asset(args.asset_id, args.note, apply=args.apply), indent=2))
            return 0
        staged: list[str] = []
        if args.resume_template:
            headings = validate_template(args.resume_template.expanduser().resolve())
            destination = TEMPLATE_DIR / TEMPLATE_NAME
            if not args.dry_run:
                copy_private(args.resume_template.expanduser().resolve(), destination, replace=args.replace)
            staged.append(f"resume template -> {destination} ({', '.join(headings)})")
        destination_dir = SOURCE_ROOT / BUCKETS[args.bucket]
        for source in args.source:
            source = source.expanduser().resolve()
            destination = destination_dir / source.name
            if not source.is_file():
                raise ValueError(f"File not found: {source}")
            if not args.dry_run:
                copy_private(source, destination, replace=args.replace)
            staged.append(f"source -> {destination}")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"dry_run": args.dry_run, "staged": staged, **status()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
