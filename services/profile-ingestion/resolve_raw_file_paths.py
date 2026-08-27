import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn  # noqa: E402


PROJECT_ROOT = Path(os.getenv("JOBOS_PROJECT_ROOT", Path.cwd())).resolve()
RAW_DIR = Path(os.getenv("JOBOS_PROFILE_RAW_DIR", PROJECT_ROOT / "data" / "profile_raw")).resolve()
PARSED_DIR = Path(os.getenv("JOBOS_PROFILE_PARSED_DIR", PROJECT_ROOT / "data" / "profile_parsed")).resolve()

COMPONENT_NAME = "source_file_path_resolver"
TASK_TYPE = "resolve_raw_file_paths"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def find_original_file(file_name: str) -> Optional[Path]:
    target = normalize_name(file_name)
    matches = []

    for p in RAW_DIR.rglob("*"):
        if p.is_file() and normalize_name(p.name) == target:
            matches.append(p)

    if len(matches) == 1:
        return matches[0]

    return None


def find_parsed_text(file_name: str) -> Optional[Path]:
    stem = Path(file_name).stem
    candidates = []

    for p in PARSED_DIR.rglob("*.txt"):
        if p.name == f"{stem}.txt":
            candidates.append(p)
        elif p.name.startswith(stem + ".") and p.name.endswith(".txt"):
            candidates.append(p)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return candidates[0]

    return None


def main() -> int:
    print("===== SOURCE FILE PATH RESOLVER =====")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Raw dir:      {RAW_DIR}")
    print(f"Parsed dir:   {PARSED_DIR}")
    print("")

    if not RAW_DIR.exists():
        raise SystemExit(f"Raw dir does not exist: {RAW_DIR}")

    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  id,
                  file_name,
                  sha256,
                  source
                FROM raw_files
                WHERE source = 'local_profile_ingestion'
                  AND is_active = true
                ORDER BY file_name;
                """
            )
            rows = cur.fetchall()

            resolved = 0
            missing_original = 0
            sha_mismatch = 0
            missing_parsed = 0

            for raw_file_id, file_name, db_sha256, source in rows:
                original = find_original_file(file_name)
                parsed = find_parsed_text(file_name)

                status = "verified"
                error_parts = []

                file_size = None
                storage_url = None
                original_path = None
                parsed_path = None

                if original is None:
                    status = "missing_original"
                    missing_original += 1
                    error_parts.append("Original file not found in data/profile_raw.")
                else:
                    original_path = str(original)
                    storage_url = original.as_uri()
                    file_size = original.stat().st_size

                    actual_sha = sha256_file(original)
                    if db_sha256 and actual_sha.lower() != db_sha256.lower():
                        status = "sha_mismatch"
                        sha_mismatch += 1
                        error_parts.append(
                            f"SHA mismatch: db={db_sha256}, actual={actual_sha}"
                        )

                if parsed is None:
                    if status == "verified":
                        status = "missing_parsed_text"
                    missing_parsed += 1
                    error_parts.append("Parsed text file not found in data/profile_parsed.")
                else:
                    parsed_path = str(parsed)

                cur.execute(
                    """
                    UPDATE raw_files
                    SET
                      storage_url = COALESCE(%s, storage_url),
                      original_local_path = %s,
                      parsed_text_path = %s,
                      file_size_bytes = %s,
                      path_status = %s,
                      path_error = %s,
                      last_path_verified_at = now()
                    WHERE id = %s;
                    """,
                    (
                        storage_url,
                        original_path,
                        parsed_path,
                        file_size,
                        status,
                        "\n".join(error_parts) if error_parts else None,
                        raw_file_id,
                    ),
                )

                cur.execute(
                    """
                    INSERT INTO component_runs (
                      component_name,
                      task_type,
                      source_file_id,
                      input_json,
                      output_json,
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
                      'completed',
                      'deterministic',
                      'path_resolver_v1',
                      0,
                      0,
                      0,
                      now()
                    );
                    """,
                    (
                        COMPONENT_NAME,
                        TASK_TYPE,
                        raw_file_id,
                        Jsonb({"file_name": file_name, "source": source}),
                        Jsonb(
                            {
                                "path_status": status,
                                "original_local_path": original_path,
                                "parsed_text_path": parsed_path,
                                "file_size_bytes": file_size,
                                "path_error": "\n".join(error_parts) if error_parts else None,
                            }
                        ),
                    ),
                )

                if status == "verified":
                    resolved += 1

            conn.commit()

    print(f"Files checked:      {len(rows)}")
    print(f"Verified:           {resolved}")
    print(f"Missing original:   {missing_original}")
    print(f"SHA mismatch:       {sha_mismatch}")
    print(f"Missing parsed txt: {missing_parsed}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
