#!/usr/bin/env python3
"""Stage private profile files and a fixed resume template for a new JobOS machine.

This local helper never uploads, parses into the database, calls an LLM, or
approves evidence.  It makes the otherwise easy-to-miss private directories
and then prints the next explicit review steps.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
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
        from docx import Document
    except ImportError as exc:
        raise ValueError("Install Python requirements before validating the Word template.") from exc
    document = Document(path)
    headings = {paragraph.text.strip().upper() for paragraph in document.paragraphs}
    missing = {"PROJECTS", "CERTIFICATIONS", "SKILLS"} - headings
    if missing:
        raise ValueError(f"Template is missing required fixed headings: {', '.join(sorted(missing))}.")
    return sorted(headings & {"PROJECTS", "CERTIFICATIONS", "SKILLS"})


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage private JobOS profile inputs on a new machine.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
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
