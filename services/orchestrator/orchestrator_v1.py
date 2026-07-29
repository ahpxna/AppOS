"""
L1 -- CONTROL PLANE ORCHESTRATOR

Three responsibilities:
  intake   -- capture a job, dedupe by jd_hash, land it at step 'intake'
  filter   -- run deterministic rules before any model is called
  advance  -- move applications through the state machine by invoking L5/L6

Design rules:
  * Every transition is validated against pipeline_transitions. A step change
    that is not an explicitly declared edge is refused, so no bug can route
    around the truth checker or the approval gate.
  * Transitions marked automated=false in the database cannot be performed by
    this orchestrator at all. Reaching 'submitted' requires a human.
  * The no-LLM filter runs first and is pure string matching. Rejecting a
    posting for being unpaid or requiring a clearance costs nothing; that
    decision should never burn model time.

Usage:
  python services/orchestrator/orchestrator_v1.py intake \
      --jd-file data/job_jds/test_jd.txt --company Acme --job-title "Analyst"
  python services/orchestrator/orchestrator_v1.py filter --all
  python services/orchestrator/orchestrator_v1.py advance --application-id <uuid>
  python services/orchestrator/orchestrator_v1.py advance --all --apply
  python services/orchestrator/orchestrator_v1.py board
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

ORCHESTRATOR_VERSION = "orchestrator_v1_state_machine_2026_07_28"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = sys.executable

FIT_SCRIPT = os.path.join(REPO_ROOT, "services", "job-analysis", "analyze_job_fit_v1.py")
DOCGEN_SCRIPT = os.path.join(REPO_ROOT, "services", "document-generation", "generate_documents_v1.py")
VERIFY_SCRIPT = os.path.join(REPO_ROOT, "services", "document-generation", "verify_document_truth_v1.py")


# ---------------------------------------------------------------- transitions

def transition(
    cur, *, application_id: str, to_step: str, actor: str,
    reason: str = "", detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Validated state change. Raises if the edge is not declared."""
    cur.execute(
        "SELECT current_step FROM applications WHERE id = %s;", (application_id,)
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Application not found: {application_id}")
    from_step = row[0]

    if from_step == to_step:
        return

    cur.execute(
        "SELECT automated FROM pipeline_transitions WHERE from_step = %s AND to_step = %s;",
        (from_step, to_step),
    )
    edge = cur.fetchone()
    if not edge:
        raise RuntimeError(
            f"Illegal transition {from_step!r} -> {to_step!r}. "
            "Not declared in pipeline_transitions."
        )
    if not edge[0] and actor == "orchestrator":
        raise RuntimeError(
            f"Transition {from_step!r} -> {to_step!r} requires a human actor. "
            "The orchestrator may not perform it."
        )

    cur.execute(
        "UPDATE applications SET current_step = %s, updated_at = now() WHERE id = %s;",
        (to_step, application_id),
    )
    cur.execute(
        """
        INSERT INTO pipeline_events
          (application_id, from_step, to_step, actor, reason, detail_json)
        VALUES (%s, %s, %s, %s, %s, %s);
        """,
        (application_id, from_step, to_step, actor, reason, Jsonb(detail or {})),
    )
    print(f"    {from_step} -> {to_step}  ({reason})")


# ---------------------------------------------------------------- intake

def intake(cur, *, jd_text: str, company: str, job_title: str,
           job_url: Optional[str], source: str, channel: str) -> Optional[str]:
    jd_text = jd_text.strip()
    jd_hash = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()

    cur.execute(
        "SELECT id::text, company, job_title FROM applications WHERE jd_hash = %s;",
        (jd_hash,),
    )
    existing = cur.fetchone()
    if existing:
        print(f"  duplicate of {existing[0]} ({existing[1]} / {existing[2]}); skipped")
        return None

    cur.execute(
        """
        INSERT INTO applications
          (source, company, job_title, job_url, jd_text, jd_hash,
           current_step, status, intake_channel, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'intake', 'active', %s, now(), now())
        RETURNING id::text;
        """,
        (source, company, job_title, job_url, jd_text, jd_hash, channel),
    )
    app_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO pipeline_events
          (application_id, from_step, to_step, actor, reason, detail_json)
        VALUES (%s, NULL, 'intake', 'orchestrator', 'Job captured.', %s);
        """,
        (app_id, Jsonb({"channel": channel, "source": source, "jd_hash": jd_hash})),
    )
    print(f"  intake: {app_id}  {company} / {job_title}")
    return app_id


# ---------------------------------------------------------------- no-LLM filter

def load_rules(cur) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT id::text, rule_name, rule_type, pattern, action, reason
        FROM no_llm_filter_rules WHERE enabled = true;
        """
    )
    return [
        {"id": r[0], "rule_name": r[1], "rule_type": r[2],
         "pattern": r[3], "action": r[4], "reason": r[5]}
        for r in cur.fetchall()
    ]


def apply_rules(rules, *, jd_text: str, job_title: str,
                company: str) -> Dict[str, Any]:
    hits, flags = [], []
    for rule in rules:
        matched = False
        try:
            if rule["rule_type"] == "min_jd_length":
                matched = len(jd_text) < int(rule["pattern"])
            elif rule["rule_type"] == "jd_regex":
                matched = re.search(rule["pattern"], jd_text) is not None
            elif rule["rule_type"] == "title_regex":
                matched = re.search(rule["pattern"], job_title or "") is not None
            elif rule["rule_type"] == "location_regex":
                matched = re.search(rule["pattern"], jd_text) is not None
            elif rule["rule_type"] == "company_blocklist":
                matched = (company or "").strip().lower() in {
                    c.strip().lower() for c in rule["pattern"].split(",")
                }
        except re.error as e:
            print(f"    rule {rule['rule_name']} has a bad pattern: {e}")
            continue

        if matched:
            entry = {"rule_name": rule["rule_name"], "reason": rule["reason"]}
            (hits if rule["action"] == "reject" else flags).append(entry)

    return {
        "rejected": bool(hits),
        "reject_hits": hits,
        "flags": flags,
        "rules_evaluated": len(rules),
    }


def run_filter(cur, application_id: str, rules) -> bool:
    """Returns True if the application survives."""
    cur.execute(
        "SELECT company, job_title, jd_text FROM applications WHERE id = %s;",
        (application_id,),
    )
    company, job_title, jd_text = cur.fetchone()
    result = apply_rules(rules, jd_text=jd_text or "",
                         job_title=job_title or "", company=company or "")

    cur.execute(
        "UPDATE applications SET filter_result = %s WHERE id = %s;",
        (Jsonb(result), application_id),
    )

    for hit in result["reject_hits"]:
        cur.execute(
            "UPDATE no_llm_filter_rules SET hit_count = hit_count + 1 WHERE rule_name = %s;",
            (hit["rule_name"],),
        )

    if result["rejected"]:
        reasons = "; ".join(h["reason"] for h in result["reject_hits"])
        transition(cur, application_id=application_id, to_step="filtered_out",
                   actor="no_llm_filter", reason=reasons, detail=result)
        return False

    transition(cur, application_id=application_id, to_step="screened",
               actor="no_llm_filter",
               reason=f"Passed {result['rules_evaluated']} rules.", detail=result)
    return True


# ---------------------------------------------------------------- subprocess steps

TRANSIENT_MARKERS = (
    "Connection refused", "URLError", "Ollama request failed",
    "timed out", "Temporary failure in name resolution",
    "Connection reset", "ConnectionError",
)


def run_step(script: str, args: List[str]) -> tuple[bool, str, bool]:
    """Returns (ok, output, is_transient)."""
    proc = subprocess.run(
        [PYTHON, script, *args], capture_output=True, text=True, timeout=1800
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    transient = (not ok) and any(m in out for m in TRANSIENT_MARKERS)
    return ok, out, transient
def record_failure(cur, application_id: str, step: str, output: str,
                   *, transient: bool) -> None:
    cur.execute(
        """
        UPDATE applications
        SET error_count = error_count + 1,
            last_error_step = %s, last_error_at = now(), last_error = %s
        WHERE id = %s;
        """,
        (step, output[-1000:], application_id),
    )
    if transient:
        # Leave the application where it is. A dependency being briefly
        # unavailable is not a reason to change its pipeline position.
        cur.execute(
            """
            INSERT INTO pipeline_events
              (application_id, from_step, to_step, actor, reason, detail_json)
            VALUES (%s, %s, %s, 'orchestrator', %s, %s);
            """,
            (application_id, step, step,
             "Transient failure; staying put for retry.",
             Jsonb({"output": output[-2000:], "transient": True})),
        )
        print("    transient failure; step unchanged, will retry next run")
    else:
        transition(cur, application_id=application_id, to_step="error",
                   actor="orchestrator", reason="Unrecoverable failure.",
                   detail={"output": output[-2000:], "failed_step": step})

def advance_one(cur, application_id: str, *, apply: bool) -> None:
    cur.execute(
        """
        SELECT a.current_step, a.company, a.job_title, ps.is_terminal, ps.requires_human
        FROM applications a
        JOIN pipeline_steps ps ON ps.step = a.current_step
        WHERE a.id = %s;
        """,
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        print(f"  not found: {application_id}")
        return
    step, company, job_title, is_terminal, requires_human = row

    print(f"\n  {application_id}  {company} / {job_title}  [{step}]")

    if is_terminal:
        print("    terminal; nothing to do")
        return
    if requires_human:
        print("    waiting on a human; the orchestrator will not act")
        return
    if not apply:
        print("    (dry run: would run the next step)")
        return

    if step == "intake":
        rules = load_rules(cur)
        run_filter(cur, application_id, rules)

    elif step == "screened":
        ok, out, transient = run_step(FIT_SCRIPT, ["--application-id", application_id, "--apply"])
        if not ok:
            record_failure(cur, application_id, step, out, transient=transient)
            return
        cur.execute(
            "SELECT fit_decision, fit_score FROM job_fit_analyses "
            "WHERE application_id = %s ORDER BY created_at DESC LIMIT 1;",
            (application_id,),
        )
        r = cur.fetchone()
        decision, score = (r[0], r[1]) if r else ("reject", 0)
        cur.execute(
            "UPDATE applications SET fit_score = %s, fit_decision = %s WHERE id = %s;",
            (score, decision, application_id),
        )
        transition(
            cur, application_id=application_id,
            to_step="fit_rejected" if decision == "reject" else "fit_analyzed",
            actor="orchestrator", reason=f"Fit {score} / {decision}",
        )
    elif step == "fit_analyzed":
        ok, out, transient = run_step(DOCGEN_SCRIPT,
                                      ["--application-id", application_id,
                                       "--doc-type", "resume", "--apply"])
        if not ok:
            record_failure(cur, application_id, step, out, transient=transient)
            return
        transition(cur, application_id=application_id, to_step="docs_generated",
                   actor="orchestrator", reason="Resume draft generated.")



    elif step == "docs_generated":
        ok, out, transient = run_step(VERIFY_SCRIPT, ["--pending", "--apply"])
        if not ok and transient:
            record_failure(cur, application_id, step, out, transient=True)
            return
        # A non-zero exit with qa_status='fail' is a real verdict, not a crash,
        # so only transient failures short-circuit here.

        cur.execute(
            """
            SELECT id::text, qa_status, revision_round
            FROM generated_documents
            WHERE application_id = %s
            ORDER BY (qa_status = 'pass') DESC, created_at DESC
            LIMIT 1;
            """,
            (application_id,),
        )
        r = cur.fetchone()
        doc_id, qa, rround = (r[0], r[1], r[2]) if r else (None, None, 0)

        if qa == "pass":
            transition(cur, application_id=application_id, to_step="docs_verified",
                       actor="truth_quality_checker", reason="All claims supported.")
            transition(cur, application_id=application_id, to_step="awaiting_approval",
                       actor="orchestrator", reason="Queued for human approval.")
        elif qa is None and rround > 0:
            # The verifier stripped ungrounded claims and produced a revision.
            # It is queued for QA; verify it on the next pass.
            print(f"    revision round {rround} created; awaiting verification")
        elif qa is None:
            record_failure(cur, application_id, step,
                           "Verifier did not record a qa_status.", transient=True)
        else:
            transition(cur, application_id=application_id, to_step="docs_failed_qa",
                       actor="truth_quality_checker",
                       reason=f"qa_status={qa!r}; claims could not be grounded.")

    else:
        print(f"    no automated action defined for step {step!r}")


# ---------------------------------------------------------------- commands

def cmd_intake(conn, args) -> int:
    if args.jd_file:
        with open(args.jd_file, "r", encoding="utf-8") as f:
            jd_text = f.read()
    else:
        jd_text = sys.stdin.read()

    if not jd_text.strip():
        print("ERROR: empty job description.")
        return 1

    with conn.cursor() as cur:
        app_id = intake(
            cur, jd_text=jd_text, company=args.company, job_title=args.job_title,
            job_url=args.job_url, source=args.source, channel=args.channel,
        )
        if app_id and args.filter:
            run_filter(cur, app_id, load_rules(cur))
    conn.commit()
    return 0


def cmd_filter(conn, args) -> int:
    with conn.cursor() as cur:
        if args.application_id:
            ids = [args.application_id]
        else:
            cur.execute("SELECT id::text FROM applications WHERE current_step = 'intake';")
            ids = [r[0] for r in cur.fetchall()]

        if not ids:
            print("Nothing at step 'intake'.")
            return 0

        rules = load_rules(cur)
        print(f"{len(rules)} enabled rules, {len(ids)} application(s)\n")
        survived = 0
        for app_id in ids:
            cur.execute("SELECT company, job_title FROM applications WHERE id = %s;", (app_id,))
            c, t = cur.fetchone()
            print(f"  {c} / {t}")
            if run_filter(cur, app_id, rules):
                survived += 1

        if not args.apply:
            conn.rollback()
            print(f"\nDRY RUN. {survived}/{len(ids)} would survive. Nothing committed.")
            return 0
        conn.commit()
        print(f"\n{survived}/{len(ids)} survived the filter.")
    return 0


def cmd_advance(conn, args) -> int:
    with conn.cursor() as cur:
        if args.application_id:
            ids = [args.application_id]
        else:
            cur.execute(
                """
                SELECT a.id::text FROM applications a
                JOIN pipeline_steps ps ON ps.step = a.current_step
                WHERE ps.is_terminal = false AND ps.requires_human = false
                ORDER BY ps.sort_order, a.updated_at;
                """
            )
            ids = [r[0] for r in cur.fetchall()]

        if not ids:
            print("Nothing to advance.")
            return 0

        for app_id in ids:
            try:
                advance_one(cur, app_id, apply=args.apply)
                if args.apply:
                    conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"    error: {type(e).__name__}: {e}")

        if not args.apply:
            conn.rollback()
            print("\nDRY RUN. Nothing committed.")
    return 0


def cmd_board(conn, args) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT step, layer, requires_human, application_count FROM v_pipeline_board;")
        print(f"\n{'STEP':<20} {'LAYER':<6} {'HUMAN':<6} COUNT")
        print("-" * 44)
        for step, layer, human, count in cur.fetchall():
            print(f"{step:<20} {layer:<6} {'yes' if human else '':<6} {count}")

        cur.execute(
            """
            SELECT rule_name, hit_count FROM no_llm_filter_rules
            WHERE hit_count > 0 ORDER BY hit_count DESC;
            """
        )
        rows = cur.fetchall()
        if rows:
            print(f"\n{'FILTER RULE':<28} HITS")
            print("-" * 36)
            for name, hits in rows:
                print(f"{name:<28} {hits}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="JobOS L1 control plane")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("intake", help="Capture a job posting.")
    pi.add_argument("--jd-file", help="Path to the JD text. Omit to read stdin.")
    pi.add_argument("--company", required=True)
    pi.add_argument("--job-title", required=True)
    pi.add_argument("--job-url")
    pi.add_argument("--source", default="manual")
    pi.add_argument("--channel", default="cli")
    pi.add_argument("--filter", action="store_true", help="Run the filter immediately.")

    pf = sub.add_parser("filter", help="Run the no-LLM filter.")
    pf.add_argument("--application-id")
    pf.add_argument("--all", action="store_true")
    pf.add_argument("--apply", action="store_true")

    pa = sub.add_parser("advance", help="Advance the state machine.")
    pa.add_argument("--application-id")
    pa.add_argument("--all", action="store_true")
    pa.add_argument("--apply", action="store_true")

    sub.add_parser("board", help="Show pipeline status.")

    args = p.parse_args()

    print(f"===== JOBOS ORCHESTRATOR ({ORCHESTRATOR_VERSION}) =====")

    with psycopg.connect(DSN, autocommit=False) as conn:
        if args.command == "intake":
            return cmd_intake(conn, args)
        if args.command == "filter":
            return cmd_filter(conn, args)
        if args.command == "advance":
            return cmd_advance(conn, args)
        if args.command == "board":
            return cmd_board(conn, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
