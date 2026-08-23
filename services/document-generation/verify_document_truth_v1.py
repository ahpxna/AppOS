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
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

# Make `services.*` importable regardless of cwd/PYTHONPATH when this file
# is run directly. Without this, the import below raises
# ModuleNotFoundError unless the caller happens to have the repo root on
# PYTHONPATH already. Confirmed live 2026-08-01 (this file was already
# broken this way before today's fix).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.observability import emit_trace, make_trace_id
from services.common.llm_gateway import generate_text
from services.common.model_config import get_model
from services.common.resume_project_bullet_audit import (
    ResumeBulletAuditError, build_bullet_audit_prompt, load_template_bullet_baselines,
    validate_bullet_change,
)
from services.common.resume_project_header_audit import (
    ResumeHeaderAuditError, build_subtitle_audit_prompt, load_template_subtitle_baselines,
    validate_subtitle_change,
)
from services.common.config import database_dsn

DSN = database_dsn()

VERIFIER_VERSION = "truth_quality_checker_v2_structured_and_company_grounded_2026_08_23"
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = get_model("verifier")

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
    return generate_text(role="verifier", model=model, prompt=prompt,
                         local_url=ollama_url, timeout=timeout,
                         temperature=temperature, num_ctx=num_ctx)


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


def fetch_company_source_urls(cur, application_id: str) -> set[str]:
    """Load URLs from the company cache without reinterpreting company facts.

    Generated documents retain their own evidence map. This lookup only checks
    that a URL the cover-letter generator recorded came from the company's
    cache, not that it is still fresh today.
    """
    cur.execute(
        """
        SELECT crc.sources
        FROM applications a
        JOIN company_research_cache crc
          ON lower(crc.company_name) = lower(a.company)
        WHERE a.id = %s
        ORDER BY crc.last_refreshed_at DESC NULLS LAST, crc.created_at DESC;
        """,
        (application_id,),
    )
    return {
        url.strip()
        for (sources,) in cur.fetchall()
        for url in (sources or [])
        if isinstance(url, str) and url.strip().startswith(("https://", "http://"))
    }


def fetch_application_jd(cur, application_id: str) -> str:
    """Load the original JD for exact-quote checking in subtitle audits."""
    cur.execute("SELECT jd_text FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    return str(row[0] or "") if row else ""


def fetch_application_identity(cur, application_id: str) -> tuple[str, str]:
    cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    return (str(row[0] or ""), str(row[1] or "")) if row else ("", "")


def deterministic_cover_structure(company: str, job_title: str) -> set[str]:
    if not company.strip() or not job_title.strip():
        return set()
    return {
        f"I am applying for the {job_title.strip()} position at {company.strip()}.",
        "Thank you for considering my application.",
    }


def fetch_company_research_text(cur, application_id: str) -> str:
    """Return the cached, URL-backed company context for literal-quote audit."""
    cur.execute(
        """
        SELECT crc.summary, crc.mission, crc.products, crc.recent_news
        FROM applications a
        JOIN company_research_cache crc ON lower(crc.company_name) = lower(a.company)
        WHERE a.id = %s
        ORDER BY crc.last_refreshed_at DESC NULLS LAST, crc.created_at DESC
        LIMIT 1;
        """,
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        return ""
    return "\n".join(str(value or "") for value in row)


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


def build_company_claim_prompt(paragraph: str, company_insight: str, evidence_quote: str) -> str:
    """Check company-claim entailment separately from candidate grounding."""
    return f"""You are JobOS Company-Context Checker V1.

Determine whether the company-specific statement is supported by the exact
source excerpt. Do not use outside knowledge. A real URL is not proof that an
interpretation is true.

PARAGRAPH:
{paragraph!r}

COMPANY-SPECIFIC STATEMENT TO VERIFY:
{company_insight!r}

ONLY SOURCE EXCERPT:
{evidence_quote!r}

Return ONLY valid JSON:
{{"verdict":"supported | unsupported", "reason":"one concise reason"}}
"""


def verify_claims(
    cur, claims: List[Dict[str, Any]], *, model: str, ollama_url: str,
    timeout: int, temperature: float, num_ctx: int, verbose: bool,
    valid_company_source_urls: set[str],
    jd_text: str = "", baseline_subtitles: Optional[Dict[int, str]] = None,
    baseline_bullets: Optional[Dict[int, str]] = None,
    company_context_text: str = "",
    structural_cover_texts: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    results = []
    tokens_in = tokens_out = 0
    start = time.perf_counter()
    for i, c in enumerate(claims, 1):
        claim_text = c.get("claim", "")
        asset_id = c.get("source_asset_id")
        raw_company_urls = c.get("company_source_urls") or []
        company_urls = [url for url in raw_company_urls if isinstance(url, str)] \
            if isinstance(raw_company_urls, list) else []
        uses_company_context = bool(c.get("uses_company_context")) or bool(company_urls)
        company_evidence = {
            "uses_company_context": uses_company_context,
            "company_source_urls": company_urls,
        }
        if uses_company_context and (
            not company_urls or any(url not in valid_company_source_urls for url in company_urls)
        ):
            results.append({
                "claim": claim_text, "source_asset_id": asset_id,
                "verdict": "unsupported",
                "reason": "Company-context claim has no known company research source URL.",
                "safe_rewrite": "",
                **company_evidence,
            })
            continue

        if c.get("answerable") is False:
            results.append({
                "claim": claim_text, "source_asset_id": None,
                "verdict": "supported",
                "reason": "Flagged for user input; makes no factual claim.",
                "safe_rewrite": "",
                **company_evidence,
            })
            continue

        if not asset_id or asset_id == "none":
            is_valid_structure = (
                c.get("kind") == "cover_letter_structure"
                and claim_text in (structural_cover_texts or set())
                and not uses_company_context
            )
            results.append({
                "claim": claim_text, "source_asset_id": None,
                "verdict": "supported" if is_valid_structure else "rule_violation",
                "reason": (
                    "Deterministic cover-letter structure with no factual claim."
                    if is_valid_structure else
                    "Uncited text is not an approved deterministic structural cover-letter sentence."
                ),
                "safe_rewrite": "",
                **company_evidence,
            })
            continue

        asset = fetch_asset(cur, asset_id)
        if not asset:
            results.append({
                "claim": claim_text, "source_asset_id": asset_id,
                "verdict": "unsupported",
                "reason": "Cited asset is not approved or no longer exists.",
                "safe_rewrite": "",
                **company_evidence,
            })
            continue

        is_subtitle_change = c.get("kind") == "resume_project_subtitle_change"
        is_bullet_change = c.get("kind") == "resume_project_bullet_change"
        is_cover_evidence = c.get("kind") == "cover_letter_evidence"
        if is_subtitle_change or is_bullet_change:
            asset_source = "\n\n".join(str(asset.get(key) or "") for key in ("summary", "bullets", "positioning"))
            audit_problems = (
                validate_subtitle_change(c, baseline_subtitles=baseline_subtitles or {}, jd_text=jd_text, asset_source=asset_source)
                if is_subtitle_change else
                validate_bullet_change(c, baseline_bullets=baseline_bullets or {}, jd_text=jd_text, asset_source=asset_source)
            )
            if audit_problems:
                results.append({
                    "claim": claim_text, "source_asset_id": asset_id, "asset_title": asset["title"],
                    "kind": c.get("kind"), "verdict": "rule_violation",
                    "reason": "; ".join(audit_problems), "safe_rewrite": "",
                    "resume_change_audit": {"passed": False, "problems": audit_problems}, **company_evidence,
                })
                continue
            prompt = build_subtitle_audit_prompt(c, asset, jd_text) if is_subtitle_change else build_bullet_audit_prompt(c, asset, jd_text)
        elif is_cover_evidence:
            asset_source = "\n\n".join(str(asset.get(key) or "") for key in ("summary", "bullets", "positioning"))
            jd_quote = str(c.get("jd_requirement_quote") or "").strip()
            evidence_quote = str(c.get("candidate_evidence_quote") or "").strip()
            cover_problems = []
            if len(jd_quote) < 8 or jd_quote.casefold() not in jd_text.casefold():
                cover_problems.append("JD requirement quote is absent from the original job description.")
            if len(evidence_quote) < 8 or evidence_quote.casefold() not in asset_source.casefold():
                cover_problems.append("Candidate evidence quote is absent from the cited approved asset.")
            if c.get("uses_company_context") and (
                len(str(c.get("company_evidence_quote") or "").strip()) < 8
                or str(c.get("company_evidence_quote") or "").casefold() not in company_context_text.casefold()
            ):
                cover_problems.append("Company evidence quote is absent from sourced company research context.")
            if cover_problems:
                results.append({
                    "claim": claim_text, "source_asset_id": asset_id, "asset_title": asset["title"],
                    "kind": "cover_letter_evidence", "verdict": "rule_violation",
                    "reason": "; ".join(cover_problems), "safe_rewrite": "",
                    "cover_evidence_audit": {"passed": False, "problems": cover_problems}, **company_evidence,
                })
                continue
            if uses_company_context:
                company_prompt = build_company_claim_prompt(
                    claim_text,
                    str(c.get("company_insight") or ""),
                    str(c.get("company_evidence_quote") or ""),
                )
                company_raw = ollama_generate(
                    model=model, prompt=company_prompt, ollama_url=ollama_url,
                    timeout=timeout, temperature=0.0, num_ctx=num_ctx,
                )
                tokens_in += estimate_tokens(company_prompt)
                tokens_out += estimate_tokens(company_raw)
                try:
                    company_verdict = extract_json_object(company_raw).get("verdict")
                except (ValueError, json.JSONDecodeError):
                    company_verdict = "unsupported"
                if company_verdict != "supported":
                    results.append({
                        "claim": claim_text, "source_asset_id": asset_id, "asset_title": asset["title"],
                        "kind": "cover_letter_evidence", "verdict": "rule_violation",
                        "reason": "Company-specific claim is not entailed by its cited source excerpt.",
                        "safe_rewrite": "",
                        "cover_evidence_audit": {"passed": False, "company_entailment": False},
                        **company_evidence,
                    })
                    continue
            prompt = build_claim_prompt(claim_text, asset)
        else:
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
        if (is_subtitle_change or is_bullet_change or is_cover_evidence) and verdict != "supported":
            # Resume template changes cannot take the lossy revision path:
            # they must remain attached to their fixed slot and full audit.
            verdict = "rule_violation"

        results.append({
            "claim": claim_text, "source_asset_id": asset_id,
            "asset_title": asset["title"], "kind": c.get("kind", "claim"), "verdict": verdict,
            "reason": parsed.get("reason", ""),
            "safe_rewrite": parsed.get("safe_rewrite", ""),
            **({"resume_change_audit": {
                "passed": verdict == "supported", "slot": c.get("slot"),
                "previous_text": c.get("previous_subtitle") if is_subtitle_change else c.get("previous_bullet"),
                "jd_requirement_quote": c.get("jd_requirement_quote"),
                "project_evidence_quote": c.get("project_evidence_quote"),
                "word_change_rationale": c.get("word_change_rationale"),
                "why_better": c.get("why_better"),
            }} if (is_subtitle_change or is_bullet_change) else {}),
            **({"cover_evidence_audit": {
                "passed": verdict == "supported", "jd_requirement_quote": c.get("jd_requirement_quote"),
                "candidate_evidence_quote": c.get("candidate_evidence_quote"),
                "company_insight": c.get("company_insight"),
                "company_evidence_quote": c.get("company_evidence_quote"),
                "why_company_fit": c.get("why_company_fit"),
            }} if is_cover_evidence else {}),
            **company_evidence,
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
            | ({
                "uses_company_context": r.get("uses_company_context", False),
                "company_source_urls": r.get("company_source_urls", []),
            } if r.get("uses_company_context") else {})
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
    try:
        baseline_subtitles = load_template_subtitle_baselines() if doc["doc_type"] == "resume" else None
        baseline_bullets = load_template_bullet_baselines() if doc["doc_type"] == "resume" else None
    except (ResumeHeaderAuditError, ResumeBulletAuditError) as exc:
        save_qa(cur, document_id=document_id, qa_status="fail",
                report={"verifier_version": VERIFIER_VERSION, "error": str(exc)})
        return {"qa_status": "fail", "results": []}
    results = verify_claims(
        cur, claims, model=args.model, ollama_url=args.ollama_url,
        timeout=args.timeout, temperature=args.temperature,
        num_ctx=args.ctx, verbose=verbose,
        valid_company_source_urls=fetch_company_source_urls(cur, doc["application_id"]),
        jd_text=fetch_application_jd(cur, doc["application_id"]), baseline_subtitles=baseline_subtitles,
        baseline_bullets=baseline_bullets,
        company_context_text=fetch_company_research_text(cur, doc["application_id"]),
        structural_cover_texts=deterministic_cover_structure(
            *fetch_application_identity(cur, doc["application_id"])
        ),
    )
    elapsed = time.time() - start

    qa_status = decide_qa_status(results)
    if doc["doc_type"] == "resume" and qa_status == "revise":
        # The generic revision builder flattens resume evidence into markdown
        # and loses immutable template slots/subtitle audits.  Regenerate the
        # structured resume instead of persisting a lossy child revision.
        qa_status = "fail"
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
