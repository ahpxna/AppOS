#!/usr/bin/env python3
"""Build or verify a deterministic source release from the tracked JobOS tree.

Release packaging is intentionally separate from developer-tree verification:
`.git`, caches, local runtimes, secrets and personal data never enter the
artifact.  The archive carries an internal SHA-256 manifest so an extracted
package can be verified without Git history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "RELEASE_MANIFEST.json"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


class ReleaseBuildError(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tracked_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("Building a release requires a Git checkout with tracked-file provenance.") from exc
    if result.returncode != 0:
        raise ReleaseBuildError("Building a release requires a Git checkout with tracked-file provenance.")
    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        # The source-tree manifest is release policy.  The archive receives a
        # freshly generated integrity manifest at the same path below; keeping
        # both would create duplicate ZIP entries and unverifiable provenance.
        if rel.as_posix() == MANIFEST_NAME:
            continue
        path = ROOT / rel
        if path.is_file():
            paths.append(rel)
    return sorted(paths, key=lambda p: p.as_posix())


def _release_policy() -> dict[str, object]:
    """Load the committed policy that accompanies every generated manifest."""
    path = ROOT / MANIFEST_NAME
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{MANIFEST_NAME} must be a valid committed release policy.") from exc
    if not isinstance(policy, dict) or policy.get("manifest_kind") != "release_policy":
        raise ReleaseBuildError(f"{MANIFEST_NAME} is not a valid release policy.")
    return policy


def _source_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True,
            check=False, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _require_clean_tree() -> None:
    """Refuse provenance claims from an uncommitted source tree.

    ``git ls-files`` reads the working tree while ``rev-parse HEAD`` names the
    last commit.  Packaging dirty files under that commit id would create a
    reproducible archive with false provenance, so official release builds
    require both tracked and untracked state to be clean.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT, text=True, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("Building a release requires a readable clean Git checkout.") from exc
    if result.returncode != 0:
        raise ReleaseBuildError("Building a release requires a readable clean Git checkout.")
    if result.stdout.strip():
        raise ReleaseBuildError(
            "Refusing to build from a dirty source tree; commit/stash all changes first so RELEASE_MANIFEST provenance is exact."
        )


def _forbidden_path(rel: Path) -> bool:
    parts = rel.parts
    name = rel.name
    return (
        ".git" in parts or ".venv" in parts or "__pycache__" in parts or ".pytest_cache" in parts
        or name.endswith((".pyc", ".pyo")) or name == ".env" or name.startswith(".env.") and name != ".env.example"
        or tuple(parts[:2]) in {("data", "secrets"), ("data", "openclaw-runtime"), ("data", "browser-profiles")}
    )


def _manifest_entries(files: list[Path]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for rel in files:
        if _forbidden_path(rel):
            raise ReleaseBuildError(f"Tracked release-forbidden path detected: {rel}")
        entries[rel.as_posix()] = _sha((ROOT / rel).read_bytes())
    return entries


def build(output: Path) -> Path:
    _require_clean_tree()
    files = _tracked_files()
    entries = _manifest_entries(files)
    manifest = {
        "format": 1,
        "manifest_kind": "integrity",
        "product": "JobOS",
        "source_commit": _source_commit(),
        "release_policy": _release_policy(),
        "files": entries,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in files:
            info = zipfile.ZipInfo(PurePosixPath(rel).as_posix(), FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = ((ROOT / rel).stat().st_mode & 0xFFFF) << 16
            archive.writestr(info, (ROOT / rel).read_bytes())
        info = zipfile.ZipInfo(MANIFEST_NAME, FIXED_ZIP_TIME)
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, manifest_bytes)
    os.replace(temporary, output)
    return output


def verify_manifest(root: Path) -> None:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ReleaseBuildError(f"{MANIFEST_NAME} is missing; this is not a verifiable packaged release.")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("format") != 1 or not isinstance(payload.get("files"), dict):
        raise ReleaseBuildError("Release manifest format is invalid.")
    expected: dict[str, str] = payload["files"]
    for rel_text, expected_sha in expected.items():
        rel = Path(rel_text)
        if rel.is_absolute() or ".." in rel.parts or _forbidden_path(rel):
            raise ReleaseBuildError(f"Unsafe release manifest path: {rel_text}")
        path = root / rel
        if not path.is_file():
            raise ReleaseBuildError(f"Release file missing: {rel_text}")
        actual = _sha(path.read_bytes())
        if actual != expected_sha:
            raise ReleaseBuildError(f"Release file hash mismatch: {rel_text}")
    allowed = set(expected) | {MANIFEST_NAME}
    extras = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in allowed and not any(part in {"__pycache__", ".pytest_cache"} for part in Path(rel).parts):
            extras.append(rel)
    if extras:
        raise ReleaseBuildError("Unmanifested file(s) in release tree: " + ", ".join(sorted(extras)[:20]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Build deterministic ZIP at this path.")
    mode.add_argument("--verify-manifest", type=Path, help="Verify an extracted packaged release directory.")
    args = parser.parse_args()
    try:
        if args.output:
            print(build(args.output))
        else:
            verify_manifest(args.verify_manifest.resolve())
            print("release manifest: OK")
    except (ReleaseBuildError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
