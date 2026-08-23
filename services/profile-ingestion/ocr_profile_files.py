import argparse
import json
import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.config import database_dsn

PROJECT_ROOT = Path(os.getenv("JOBOS_PROJECT_ROOT", Path.cwd())).resolve()
OCR_DIR = Path(os.getenv("JOBOS_PROFILE_OCR_DIR", PROJECT_ROOT / "data" / "profile_ocr")).resolve()

COMPONENT_NAME = "profile_ocr_engine"
TASK_TYPE_AUDIT = "audit_ocr_need"
TASK_TYPE_OCR = "run_ocr"

ENGINE = "tesseract_cli_plus_poppler"

DSN = database_dsn()


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Missing required binary: {name}. Install it first.")


def run_cmd(cmd: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def safe_stem(file_name: str) -> str:
    return Path(file_name).stem.replace("/", "_").replace(":", "_")


def get_pdf_page_count(path: Path) -> Optional[int]:
    result = run_cmd(["pdfinfo", str(path)], timeout=60)
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if line.lower().startswith("pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except Exception:
                return None
    return None


def extract_pdf_text_with_pdftotext(path: Path) -> str:
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "out.txt"
        result = run_cmd(["pdftotext", "-layout", str(path), str(out_path)], timeout=180)
        if result.returncode != 0:
            return ""
        if not out_path.exists():
            return ""
        return out_path.read_text(errors="ignore")


def audit_file(file_name: str, file_type: str, original_path: str, existing_chunk_chars: int) -> Dict:
    path = Path(original_path)
    suffix = path.suffix.lower()

    page_count = None
    extracted_text_chars = 0
    reason = "not_required"
    ocr_required = False

    if suffix == ".pdf":
        page_count = get_pdf_page_count(path)
        text = extract_pdf_text_with_pdftotext(path)
        extracted_text_chars = len(text.strip())

        pages = page_count or 1
        chars_per_page = extracted_text_chars / max(pages, 1)

        if extracted_text_chars < 300:
            ocr_required = True
            reason = "pdf_text_layer_too_small"
        elif chars_per_page < 80:
            ocr_required = True
            reason = "pdf_text_per_page_too_low"
        elif existing_chunk_chars < 300:
            ocr_required = True
            reason = "existing_chunks_too_small"
        else:
            ocr_required = False
            reason = "pdf_has_sufficient_text_layer"

    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        ocr_required = True
        reason = "image_file_requires_ocr"

    else:
        ocr_required = False
        reason = "file_type_not_ocr_target"

    return {
        "ocr_required": ocr_required,
        "reason": reason,
        "page_count": page_count,
        "extracted_text_chars": extracted_text_chars,
        "existing_chunk_chars": existing_chunk_chars,
    }


def ocr_image_to_text(image_path: Path, lang: str) -> Tuple[str, Optional[float]]:
    with tempfile.TemporaryDirectory() as td:
        out_base = Path(td) / "ocr"
        result = run_cmd(
            ["tesseract", str(image_path), str(out_base), "-l", lang, "--psm", "6", "tsv"],
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        txt_path = out_base.with_suffix(".txt")
        tsv_path = out_base.with_suffix(".tsv")

        text = txt_path.read_text(errors="ignore") if txt_path.exists() else ""

        confidences = []
        if tsv_path.exists():
            for line in tsv_path.read_text(errors="ignore").splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) >= 11:
                    try:
                        conf = float(parts[10])
                        if conf >= 0:
                            confidences.append(conf)
                    except Exception:
                        pass

        avg_conf = sum(confidences) / len(confidences) if confidences else None
        return text, avg_conf


def ocr_pdf(path: Path, out_dir: Path, raw_file_id: str, lang: str, max_pages: Optional[int]) -> Tuple[List[Dict], Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    page_prefix = out_dir / "page"

    cmd = ["pdftoppm", "-png", "-r", "200", str(path), str(page_prefix)]
    result = run_cmd(cmd, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    images = sorted(out_dir.glob("page-*.png"))
    if max_pages:
        images = images[:max_pages]

    pages = []
    full_text_parts = []

    for idx, image in enumerate(images, start=1):
        text, conf = ocr_image_to_text(image, lang)
        text = text.strip()

        pages.append(
            {
                "page_number": idx,
                "text_content": text,
                "text_char_count": len(text),
                "confidence": conf,
                "image_path": str(image),
            }
        )

        full_text_parts.append(f"\n\n--- OCR PAGE {idx} ---\n{text}")

    ocr_text_path = out_dir / "ocr_text.txt"
    ocr_text_path.write_text("".join(full_text_parts).strip() + "\n", encoding="utf-8")

    return pages, ocr_text_path


def ocr_single_image(path: Path, out_dir: Path, lang: str) -> Tuple[List[Dict], Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    text, conf = ocr_image_to_text(path, lang)
    text = text.strip()

    copied_image = out_dir / path.name
    if path.resolve() != copied_image.resolve():
        shutil.copy2(path, copied_image)

    ocr_text_path = out_dir / "ocr_text.txt"
    ocr_text_path.write_text(f"--- OCR PAGE 1 ---\n{text}\n", encoding="utf-8")

    return [
        {
            "page_number": 1,
            "text_content": text,
            "text_char_count": len(text),
            "confidence": conf,
            "image_path": str(copied_image),
        }
    ], ocr_text_path


def create_ocr_run(cur, raw_file_id, mode, input_json):
    cur.execute(
        """
        INSERT INTO raw_file_ocr_runs (
          raw_file_id,
          run_mode,
          engine,
          status,
          input_json
        )
        VALUES (%s, %s, %s, 'running', %s)
        RETURNING id;
        """,
        (raw_file_id, mode, ENGINE, Jsonb(input_json)),
    )
    return cur.fetchone()[0]


def finish_ocr_run(cur, run_id, status, output_json, error=None):
    cur.execute(
        """
        UPDATE raw_file_ocr_runs
        SET
          status = %s,
          output_json = %s,
          error_message = %s,
          finished_at = now()
        WHERE id = %s;
        """,
        (status, Jsonb(output_json), error, run_id),
    )


def log_component_run(cur, task_type, raw_file_id, input_json, output_json, status, error=None):
    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,
          task_type,
          source_file_id,
          input_json,
          output_json,
          status,
          error_message,
          model_provider,
          model_name,
          input_tokens,
          output_tokens,
          estimated_cost_usd,
          finished_at
        )
        VALUES (
          %s, %s, %s, %s, %s,
          %s, %s,
          'deterministic',
          %s,
          0, 0, 0,
          now()
        );
        """,
        (
            COMPONENT_NAME,
            task_type,
            raw_file_id,
            Jsonb(input_json),
            Jsonb(output_json),
            status,
            error,
            ENGINE,
        ),
    )


def fetch_files_for_audit(cur, only_short_ids: List[str], limit: int):
    params = []
    where = [
        "source = 'local_profile_ingestion'",
        "is_active = true",
        "path_status = 'verified'",
        "original_local_path IS NOT NULL",
    ]

    if only_short_ids:
        where.append("left(id::text, 8) = ANY(%s)")
        params.append(only_short_ids)

    params.append(limit)

    cur.execute(
        f"""
        SELECT
          id,
          file_name,
          file_type,
          original_local_path,
          COALESCE((
            SELECT sum(length(COALESCE(pc.text_content, '')))
            FROM profile_chunks pc
            WHERE pc.file_id = raw_files.id
          ), 0) AS existing_chunk_chars
        FROM raw_files
        WHERE {' AND '.join(where)}
        ORDER BY file_name
        LIMIT %s;
        """,
        params,
    )
    return cur.fetchall()


def fetch_files_for_ocr(cur, only_short_ids: List[str], limit: int):
    params = []
    where = [
        "source = 'local_profile_ingestion'",
        "is_active = true",
        "path_status = 'verified'",
        "original_local_path IS NOT NULL",
    ]

    if only_short_ids:
        where.append("left(id::text, 8) = ANY(%s)")
        params.append(only_short_ids)
    else:
        where.append("ocr_required = true")
        where.append("ocr_status IN ('required', 'failed')")

    params.append(limit)

    cur.execute(
        f"""
        SELECT
          id,
          file_name,
          file_type,
          original_local_path
        FROM raw_files
        WHERE {' AND '.join(where)}
        ORDER BY file_name
        LIMIT %s;
        """,
        params,
    )
    return cur.fetchall()


def run_audit(args) -> int:
    require_binary("pdfinfo")
    require_binary("pdftotext")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_files_for_audit(cur, args.id, args.limit)
            print(f"Files selected for audit: {len(rows)}")

            for raw_file_id, file_name, file_type, original_path, existing_chunk_chars in rows:
                print(f"- {file_name}")

                run_id = create_ocr_run(
                    cur,
                    raw_file_id,
                    "audit",
                    {
                        "file_name": file_name,
                        "original_local_path": original_path,
                    },
                )

                try:
                    result = audit_file(file_name, file_type, original_path, existing_chunk_chars)
                    status = "required" if result["ocr_required"] else "not_required"

                    cur.execute(
                        """
                        UPDATE raw_files
                        SET
                          ocr_status = %s,
                          ocr_required = %s,
                          ocr_engine = %s,
                          ocr_page_count = %s,
                          ocr_error = NULL,
                          last_ocr_at = now()
                        WHERE id = %s;
                        """,
                        (
                            status,
                            result["ocr_required"],
                            ENGINE,
                            result["page_count"],
                            raw_file_id,
                        ),
                    )

                    finish_ocr_run(cur, run_id, "completed", result)
                    log_component_run(cur, TASK_TYPE_AUDIT, raw_file_id, {"file_name": file_name}, result, "completed")

                    print(f"  OCR required: {result['ocr_required']} ({result['reason']})")

                except Exception as e:
                    error = str(e)
                    cur.execute(
                        """
                        UPDATE raw_files
                        SET
                          ocr_status = 'failed',
                          ocr_required = false,
                          ocr_engine = %s,
                          ocr_error = %s,
                          last_ocr_at = now()
                        WHERE id = %s;
                        """,
                        (ENGINE, error, raw_file_id),
                    )

                    finish_ocr_run(cur, run_id, "failed", {}, error)
                    log_component_run(cur, TASK_TYPE_AUDIT, raw_file_id, {"file_name": file_name}, {}, "failed", error)
                    print(f"  ERROR: {error}")

                conn.commit()

    return 0


def run_ocr(args) -> int:
    require_binary("tesseract")
    require_binary("pdftoppm")

    OCR_DIR.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_files_for_ocr(cur, args.id, args.limit)
            print(f"Files selected for OCR: {len(rows)}")

            for raw_file_id, file_name, file_type, original_path in rows:
                print(f"- {file_name}")

                path = Path(original_path)
                out_dir = OCR_DIR / f"{safe_stem(file_name)}.{str(raw_file_id)[:8]}"

                run_id = create_ocr_run(
                    cur,
                    raw_file_id,
                    "ocr",
                    {
                        "file_name": file_name,
                        "original_local_path": original_path,
                        "lang": args.lang,
                        "max_pages": args.max_pages,
                    },
                )

                try:
                    suffix = path.suffix.lower()

                    if suffix == ".pdf":
                        pages, ocr_text_path = ocr_pdf(path, out_dir, str(raw_file_id), args.lang, args.max_pages)
                    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
                        pages, ocr_text_path = ocr_single_image(path, out_dir, args.lang)
                    else:
                        raise RuntimeError(f"Unsupported OCR file type: {suffix}")

                    total_chars = sum(p["text_char_count"] for p in pages)
                    avg_conf_values = [p["confidence"] for p in pages if p["confidence"] is not None]
                    avg_conf = sum(avg_conf_values) / len(avg_conf_values) if avg_conf_values else None

                    for page in pages:
                        cur.execute(
                            """
                            INSERT INTO raw_file_ocr_pages (
                              raw_file_id,
                              ocr_run_id,
                              page_number,
                              text_content,
                              text_char_count,
                              confidence,
                              image_path
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (raw_file_id, ocr_run_id, page_number)
                            DO UPDATE SET
                              text_content = EXCLUDED.text_content,
                              text_char_count = EXCLUDED.text_char_count,
                              confidence = EXCLUDED.confidence,
                              image_path = EXCLUDED.image_path;
                            """,
                            (
                                raw_file_id,
                                run_id,
                                page["page_number"],
                                page["text_content"],
                                page["text_char_count"],
                                page["confidence"],
                                page["image_path"],
                            ),
                        )

                    output = {
                        "ocr_text_path": str(ocr_text_path),
                        "page_count": len(pages),
                        "ocr_char_count": total_chars,
                        "avg_confidence": avg_conf,
                    }

                    cur.execute(
                        """
                        UPDATE raw_files
                        SET
                          ocr_status = 'completed',
                          ocr_required = false,
                          ocr_engine = %s,
                          ocr_text_path = %s,
                          ocr_page_count = %s,
                          ocr_char_count = %s,
                          ocr_error = NULL,
                          last_ocr_at = now()
                        WHERE id = %s;
                        """,
                        (
                            ENGINE,
                            str(ocr_text_path),
                            len(pages),
                            total_chars,
                            raw_file_id,
                        ),
                    )

                    finish_ocr_run(cur, run_id, "completed", output)
                    log_component_run(cur, TASK_TYPE_OCR, raw_file_id, {"file_name": file_name}, output, "completed")

                    conn.commit()
                    print(f"  OCR pages: {len(pages)} chars: {total_chars}")

                except Exception as e:
                    conn.rollback()

                    with conn.cursor() as err_cur:
                        error = str(e)
                        err_cur.execute(
                            """
                            UPDATE raw_files
                            SET
                              ocr_status = 'failed',
                              ocr_engine = %s,
                              ocr_error = %s,
                              last_ocr_at = now()
                            WHERE id = %s;
                            """,
                            (ENGINE, error, raw_file_id),
                        )
                        finish_ocr_run(err_cur, run_id, "failed", {}, error)
                        log_component_run(err_cur, TASK_TYPE_OCR, raw_file_id, {"file_name": file_name}, {}, "failed", error)

                    conn.commit()
                    print(f"  ERROR: {e}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["audit", "run-needed", "run-file"])
    parser.add_argument("--id", action="append", default=[], help="raw_files short id. Can repeat.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--lang", default="eng")
    parser.add_argument("--max-pages", type=int, default=None)

    args = parser.parse_args()

    print("===== PROFILE OCR ENGINE =====")
    print(f"Mode: {args.mode}")
    print(f"OCR dir: {OCR_DIR}")
    print("")

    if args.mode == "audit":
        return run_audit(args)

    if args.mode == "run-needed":
        return run_ocr(args)

    if args.mode == "run-file":
        if not args.id:
            raise SystemExit("run-file requires --id <raw_file_short_id>")
        return run_ocr(args)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
