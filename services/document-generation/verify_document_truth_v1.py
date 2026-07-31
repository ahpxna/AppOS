"""
L6 -- TRUTH + QUALITY CHECKER

Verifies a generated document claim-by-claim against the specific asset each
claim cited. This is deliberately NOT "read the document and judge it" -- the
checker never sees the whole document at once when scoring a claim. Each claim
is shown only its own cited asset, so the model cannot borrow support from a
different asset to rescue a bad claim.

Verdicts per claim:
  supported     -- the asset states this
  overclaimed   -- the asset relates, but the claim exceeds it (scope, scale,
                   seniority, or professional-vs-academic framing)
  unsupported   -- the asset does not state this at all
  rule_violation-- the claim breaks one of the asset's do_not_overclaim_rules

Document-level qa_status:
  pass    -- every claim supported
  revise  -- some claims overclaimed/unsupported, some survive
  fail    -- nothing survives, or any rule_violation is present

Usage:
  python services/document-generation/verify_document_truth_v1.py --document-id <uuid>
  python services/document-generation/verify_document_truth_v1.py --pending --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from services.common.observability import emit_trace, make_trace_id

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

VERIFIER_VERSION = "truth_quality_checker_v1_per_claim_2026_07_28"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.getenv("JOBOS_VERIFIER_MODEL", "qwen3:8b")

FATAL_VERDICTS = {"rule_violation"}
FAILING_VERDICTS = {"overclaimed", "unsupported", "rule_violation"}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def extract_json_object(raw: str) -> Dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    cleaned = cleaned.replace("```json", "```").replace("```JSON", "```").strip()
    fence = re.search(r"```(.*?)```", cleaned, flags=re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in verifier output.")
    return json.loads(cleaned[first:last + 1])


def ollama_generate(
    *, model: str, prompt: str, ollama_url: str,
    timeout: int, temperature: float, num_ctx: int,
) -> str:
    payload = {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        ollama_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")).get("response", "")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama request failed: {e}") from e


# ---------------------------------------------------------------- data access

def fetch_document(cur, document_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT id::text, application_id::text, doc_type, version,
               content, evidence_map, revision_round
        FROM generated_documents
        WHERE id = %s;
        """,
        (document_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Document not found: {document_id}")
    return {
        "id": row[0], "application_id": row[1], "doc_type": row[2],
        "version": row[3], "content": row[4],
        "evidence_map": row[5] or {}, "revision_round": row[6],
    }


def fetch_pending_documents(cur) -> List[str]:
    cur.execute("SELECT generated_document_id::text FROM v_documents_pending_qa;")
    return [r[0] for r in cur.fetchall()]


def fetch_asset(cur, asset_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT profile_asset_id::text, asset_title, asset_type,
               job_oriented_summary, resume_bullet_bank,
               cover_letter_positioning, do_not_overclaim_rules
        FROM v_document_generation_source_assets
        WHERE profile_asset_id = %s;
        """,
        (asset_id,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "title": r[1], "type": r[2],
        "summary": r[3] or "", "bullets": r[4] or "",
        "positioning": r[5] or "", "rules": r[6] or [],
    }


# ---------------------------------------------------------------- verification

def build_claim_prompt(claim: str, asset: Dict[str, Any]) -> str:
    rules = "\n".join(f"  - {r}" for r in asset["rules"]) or "  (none recorded)"
    source = "\n\n".join(
        s for s in (asset["summary"], asset["bullets"], asset["positioning"]) if s.strip()
    )
    return f"""You are JobOS Truth Checker V1.

Decide whether ONE claim is supported by ONE source asset. You are given nothing
else. Do not use outside knowledge. Do not be generous.

THE CLAIM:
"{claim}"

THE ONLY SOURCE IT MAY DRAW ON:
Title: {asset['title']}
Type:  {asset['type']}

Source material:
{source}

Rules this asset must never violate:
{rules}

Verdict definitions:
- "supported": the source material states this. Rewording is fine; added
  substance is not.
- "overclaimed": the source is related but the claim goes beyond it. This
  includes claiming professional or production experience where the source is
  academic or coursework, inflating scope or scale, implying ownership of
  systems, or adding metrics the source does not contain.
- "unsupported": the source material does not address this claim at all.
- "rule_violation": the claim breaks one of the listed rules above.

Bias instruction: when torn between "supported" and "overclaimed", choose
"overclaimed". A false pass is far more damaging than a false flag, because a
false pass reaches an employer.

Return ONLY valid JSON:
{{
  "verdict": "supported | overclaimed | unsupported | rule_violation",
  "reason": "one sentence pointing at the specific gap or the specific support",
  "safe_rewrite": "if not supported, the strongest version the source actually justifies; otherwise empty string"
}}
"""


def verify_claims(
    cur, claims: List[Dict[str, Any]], *, model: str, ollama_url: str,
    timeout: int, temperature: float, num_ctx: int, verbose: bool,
) -> List[Dict[str, Any]]:
    results = []
    tokens_in = tokens_out = 0
    start = time.perf_counter()
    for i, c in enumerate(claims, 1):
        claim_text = c.get("claim", "")
        asset_id = c.get("source_asset_id")

        if c.get("answerable") is False:
            results.append({
                "claim": claim_text, "source_asset_id": None,
                "verdict": "supported",
                "reason": "Flagged for user input; makes no factual claim.",
                "safe_rewrite": "",
            })
            continue

        if not asset_id or asset_id == "none":
            results.append({
                "claim": claim_text, "source_asset_id": None,
                "verdict": "supported",
                "reason": "Structural text with no factual claim.",
                "safe_rewrite": "",
            })
            continue

        asset = fetch_asset(cur, asset_id)
        if not asset:
            results.append({
                "claim": claim_text, "source_asset_id": asset_id,
                "verdict": "unsupported",
                "reason": "Cited asset is not approved or no longer exists.",
                "safe_rewrite": "",
            })
            continue

        prompt = build_claim_prompt(claim_text, asset)
        raw = ollama_generate(
            model=model, prompt=prompt, ollama_url=ollama_url,
            timeout=timeout, temperature=temperature, num_ctx=num_ctx,
        )
        tokens_in += estimate_tokens(prompt)
        tokens_out += estimate_tokens(raw)
        try:
            parsed = extract_json_object(raw)
            verdict = parsed.get("verdict", "unsupported")
            if verdict not in {"supported", "overclaimed", "unsupported", "rule_violation"}:
                verdict = "unsupported"
        except (ValueError, json.JSONDecodeError):
            verdict = "unsupported"
            parsed = {"reason": "Verifier output unparseable; failing closed.",
                      "safe_rewrite": ""}

        results.append({
            "claim": claim_text, "source_asset_id": asset_id,
            "asset_title": asset["title"], "verdict": verdict,
            "reason": parsed.get("reason", ""),
            "safe_rewrite": parsed.get("safe_rewrite", ""),
        })

        if verbose:
            mark = {"supported": "OK  ", "overclaimed": "OVER",
                    "unsupported": "NONE", "rule_violation": "RULE"}[verdict]
            print(f"  [{i}/{len(claims)}] {mark}  {claim_text[:64]}")
            if verdict != "supported":
                print(f"          -> {parsed.get('reason', '')}")
    emit_trace(
        make_trace_id("docverify", claims[0].get("source_asset_id", "batch") if claims else "batch"),
        "document_truth_check",
        started_at=start,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=0.0,
        claims=len(claims),
        verdicts=len(results),
    )
    return results


def decide_qa_status(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "fail"
    verdicts = [r["verdict"] for r in results]
    if any(v in FATAL_VERDICTS for v in verdicts):
        return "fail"
    if all(v == "supported" for v in verdicts):
        return "pass"
    if not any(v == "supported" for v in verdicts):
        return "fail"
    return "revise"


def rebuild_content(doc_type: str, results: List[Dict[str, Any]]) -> str:
    """Content containing only surviving claims, for the revision round."""
    kept = [r for r in results if r["verdict"] == "supported"]
    if doc_type == "resume":
        return "\n".join(f"- {r['claim']}" for r in kept)
    return "\n\n".join(r["claim"] for r in kept)


# ---------------------------------------------------------------- persistence

def save_qa(
    cur, *, document_id: str, qa_status: str, report: Dict[str, Any],
) -> None:
    cur.execute(
        """
        UPDATE generated_documents
        SET qa_status = %s, qa_report = %s, qa_checked_at = now()
        WHERE id = %s;
        """,
        (qa_status, Jsonb(report), document_id),
    )


def insert_revision(
    cur, *, doc: Dict[str, Any], content: str, results: List[Dict[str, Any]],
) -> str:
    kept_ids = sorted({
        r["source_asset_id"] for r in results
        if r["verdict"] == "supported" and r["source_asset_id"]
    })
    evidence = {
        "doc_type": doc["doc_type"],
        "claims": [
            {"claim": r["claim"], "source_asset_id": r["source_asset_id"]}
            for r in results if r["verdict"] == "supported"
        ],
        "removed_by_truth_checker": [
            {"claim": r["claim"], "verdict": r["verdict"],
             "reason": r["reason"], "safe_rewrite": r["safe_rewrite"]}
            for r in results if r["verdict"] != "supported"
        ],
    }
    cur.execute(
        """
        INSERT INTO generated_documents (
          application_id, doc_type, version, content, format,
          asset_ids_used, evidence_map, generator_version,
          qa_status, approved, revision_of, revision_round, created_at
        )
        SELECT application_id, doc_type,
               (SELECT COALESCE(MAX(version), 0) + 1 FROM generated_documents
                 WHERE application_id = gd.application_id AND doc_type = gd.doc_type),
               %s, 'markdown', %s, %s, %s, NULL, false, gd.id, gd.revision_round + 1, now()
        FROM generated_documents gd
        WHERE gd.id = %s
        RETURNING id::text;
        """,
        (content, Jsonb(kept_ids), Jsonb(evidence), VERIFIER_VERSION, doc["id"]),
    )
    return str(cur.fetchone()[0])


# ---------------------------------------------------------------- main

def process_document(
    cur, document_id: str, args, *, verbose: bool = True
) -> Dict[str, Any]:
    doc = fetch_document(cur, document_id)
    claims = doc["evidence_map"].get("claims", [])

    print(f"\n--- {doc['doc_type']} v{doc['version']} "
          f"(round {doc['revision_round']}) -- {len(claims)} claims")

    if not claims:
        save_qa(cur, document_id=document_id, qa_status="fail",
                report={"verifier_version": VERIFIER_VERSION,
                        "error": "No claims recorded in evidence_map."})
        return {"qa_status": "fail", "results": []}

    start = time.time()
    results = verify_claims(
        cur, claims, model=args.model, ollama_url=args.ollama_url,
        timeout=args.timeout, temperature=args.temperature,
        num_ctx=args.ctx, verbose=verbose,
    )
    elapsed = time.time() - start

    qa_status = decide_qa_status(results)
    counts = {v: sum(1 for r in results if r["verdict"] == v)
              for v in ("supported", "overclaimed", "unsupported", "rule_violation")}

    report = {
        "verifier_version": VERIFIER_VERSION,
        "verifier_model": args.model,
        "claim_results": results,
        "counts": counts,
        "elapsed_seconds": round(elapsed, 1),
    }

    print(f"\n  supported={counts['supported']} overclaimed={counts['overclaimed']} "
          f"unsupported={counts['unsupported']} rule_violation={counts['rule_violation']}")
    print(f"  qa_status: {qa_status}   ({elapsed:.1f}s)")

    if not args.apply:
        return {"qa_status": qa_status, "results": results}

    save_qa(cur, document_id=document_id, qa_status=qa_status, report=report)

    if qa_status == "revise" and doc["revision_round"] < args.max_rounds:
        content = rebuild_content(doc["doc_type"], results)
        rev_id = insert_revision(cur, doc=doc, content=content, results=results)
        print(f"  revision created: {rev_id}")
        report["revision_document_id"] = rev_id
    elif qa_status == "revise":
        print(f"  max revision rounds ({args.max_rounds}) reached; stopping.")

    return {"qa_status": qa_status, "results": results}


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--document-id")
    g.add_argument("--pending", action="store_true",
                   help="Verify everything in v_documents_pending_qa.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--ctx", type=int, default=4096)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-rounds", type=int, default=2)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    print("===== TRUTH + QUALITY CHECKER V1 =====")
    print(f"Verifier: {VERIFIER_VERSION}")
    print(f"Mode:     {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Model:    {args.model}")

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            ids = ([args.document_id] if args.document_id
                   else fetch_pending_documents(cur))
            if not ids:
                print("\nNothing pending QA.")
                return 0

            summary = []
            for doc_id in ids:
                out = process_document(cur, doc_id, args)
                summary.append((doc_id, out["qa_status"]))

            if not args.apply:
                conn.rollback()
                print("\nDRY RUN ONLY. No database changes committed.")
                return 0

            conn.commit()
            print("\n===== SUMMARY =====")
            for doc_id, status in summary:
                print(f"  {status:8s}  {doc_id}")

            failed = sum(1 for _, s in summary if s == "fail")
            return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
