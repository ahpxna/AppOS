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
import secrets
import uuid
import sys
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.discovery.immigration_intelligence import record_jd_immigration_assessment
from services.common.config import database_dsn
from services.control_plane.pipeline_state import DEFAULT_PIPELINE_STATE_STORE, PipelineStateError
from services.runtime.process_runner import DEFAULT_PROCESS_RUNNER
from services.intake.posting_identity import build_posting_identity
from services.intake.source_observation import find_and_observe_existing, observe_existing_posting

ORCHESTRATOR_VERSION = "orchestrator_v1_state_machine_2026_07_28"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = sys.executable

FIT_SCRIPT = os.path.join(REPO_ROOT, "services", "job-analysis", "analyze_job_fit_v1.py")
DOCGEN_SCRIPT = os.path.join(REPO_ROOT, "services", "document-generation", "generate_documents_v1.py")
VERIFY_SCRIPT = os.path.join(REPO_ROOT, "services", "document-generation", "verify_document_truth_v1.py")
RESUME_EXPORT_SCRIPT = os.path.join(REPO_ROOT, "services", "document-generation", "render_verified_resume_v1.py")
COST_SCRIPT = os.path.join(REPO_ROOT, "services", "cost", "cost_controller_v1.py")
RESEARCH_MODULE = "services.research.company_research_v1"
MARKET_INTELLIGENCE_SCRIPT = os.path.join(
    REPO_ROOT, "services", "discovery", "market_demand_intelligence_v1.py"
)

FIT_REVIEW_TTL_HOURS = 48  # long on purpose: a human sleeps (see architecture review)
ORCHESTRATOR_LEASE_SECONDS = int(os.getenv("JOBOS_ORCHESTRATOR_LEASE_SECONDS", "7200"))
_ACTIVE_PROCESSING_RUN_ID: str | None = None


# ---------------------------------------------------------------- transitions

def transition(
    cur, *, application_id: str, to_step: str, actor: str,
    reason: str = "", detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Validated state change. Raises if the edge is not declared."""
    cur.execute("SELECT current_step FROM applications WHERE id = %s;", (application_id,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Application not found: {application_id}")
    from_step = row[0]

    if _ACTIVE_PROCESSING_RUN_ID is not None:
        cur.execute(
            """SELECT processing_run_id::text, processing_step, processing_lease_expires_at > now()
                 FROM applications WHERE id=%s;""", (application_id,)
        )
        claim = cur.fetchone()
        if (not claim or str(claim[0] or "") != _ACTIVE_PROCESSING_RUN_ID
                or str(claim[1] or "") != str(from_step) or not bool(claim[2])):
            raise RuntimeError("Orchestrator processing lease changed/expired before state completion; refusing stale completion.")

    if from_step == to_step:
        return

    try:
        DEFAULT_PIPELINE_STATE_STORE.transition(
            cur, application_id=application_id, expected_from=str(from_step), to=to_step,
            actor=actor, reason=reason, detail=detail,
            require_automated=(actor == "orchestrator"), lease_run_id=_ACTIVE_PROCESSING_RUN_ID,
        )
    except PipelineStateError as exc:
        raise RuntimeError(str(exc)) from exc
    print(f"    {from_step} -> {to_step}  ({reason})")


# ---------------------------------------------------------------- intake

def intake(cur, *, jd_text: str, company: str, job_title: str,
           job_url: Optional[str], source: str, channel: str) -> Optional[str]:
    jd_text = jd_text.strip()
    identity = build_posting_identity(
        company=company, job_title=job_title, jd_text=jd_text, job_url=job_url,
    )
    job_url, jd_hash = identity.canonical_url, identity.jd_hash
    existing, _observation = find_and_observe_existing(
        cur, identity=identity, source_name=source or "orchestrator_intake", jd_text=jd_text,
        company=company, job_title=job_title, metadata={"intake_channel": channel},
    )
    if existing:
        print(f"  duplicate of {existing[0]} ({existing[1]} / {existing[2]}); source observation recorded")
        return None

    cur.execute(
        """
        INSERT INTO applications
          (source, company, job_title, job_url, jd_text, jd_hash,
           current_step, status, intake_channel, ats_type, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'intake', 'active', %s, %s, now(), now())
        RETURNING id::text;
        """,
        (source, company, job_title, job_url, jd_text, jd_hash, channel, identity.ats_type),
    )
    app_id = cur.fetchone()[0]
    observe_existing_posting(
        cur, application_id=app_id, source_name=source or "orchestrator_intake",
        company=company, job_title=job_title, job_url=job_url, jd_text=jd_text, jd_hash=jd_hash,
        metadata={"intake_channel": channel, "initial": True},
    )
    immigration = record_jd_immigration_assessment(cur, app_id, jd_text)

    cur.execute(
        """
        INSERT INTO pipeline_events
          (application_id, from_step, to_step, actor, reason, detail_json)
        VALUES (%s, NULL, 'intake', 'orchestrator', 'Job captured.', %s);
        """,
        (app_id, Jsonb({"channel": channel, "source": source, "jd_hash": jd_hash,
                        "ats_type": identity.ats_type, "canonical_job_url": job_url,
                        "immigration_assessment": immigration})),
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
    """Run a legacy script path from the repository root."""
    result = DEFAULT_PROCESS_RUNNER.run(
        [PYTHON, script, *args], cwd=REPO_ROOT, env=_subprocess_env(args), timeout_s=1800,
    )
    return result.ok, result.output + (f"\n{result.start_error}" if result.start_error else ""), result.transient


def run_module(module: str, args: List[str]) -> tuple[bool, str, bool]:
    """Run internal package entrypoints with import semantics intact."""
    result = DEFAULT_PROCESS_RUNNER.run(
        [PYTHON, "-m", module, *args], cwd=REPO_ROOT, env=_subprocess_env(args), timeout_s=1800,
    )
    return result.ok, result.output + (f"\n{result.start_error}" if result.start_error else ""), result.transient
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


# ---------------------------------------------------------------- cost gate

def check_cost_budget(task: str, *, application_id: str | None = None) -> tuple[bool, str]:
    """Ask the cost controller whether this task type may run right now,
    and increment its counter if so. Returns (allowed, output_tail)."""
    argv = [PYTHON, COST_SCRIPT, "check", "--task", task, "--increment"]
    if application_id and task == "full_pipeline":
        argv.extend(["--subject-type", "application", "--subject-id", application_id])
    result = DEFAULT_PROCESS_RUNNER.run(argv, timeout_s=30)
    return result.ok, result.output + (f"\n{result.start_error}" if result.start_error else "")


def profile_prerequisite_blockers(cur) -> list[str]:
    """Return missing profile prerequisites without mutating pipeline state."""
    blockers: list[str] = []
    cur.execute("SELECT count(*) FROM profile_assets WHERE status='approved';")
    if int(cur.fetchone()[0]) == 0:
        blockers.append("no approved profile assets")
    cur.execute("SELECT count(*) FROM profile_capabilities WHERE status='approved';")
    if int(cur.fetchone()[0]) == 0:
        blockers.append("no approved profile capabilities")
    cur.execute("SELECT count(*) FROM profile_briefs WHERE is_stale=false;")
    if int(cur.fetchone()[0]) == 0:
        blockers.append("no fresh profile briefs")
    required = {
        "base_fit_check_support", "base_resume_generation", "base_cover_letter_generation",
        "base_short_answer_generation", "base_interview_prep", "base_message_reply",
    }
    cur.execute(
        """SELECT purpose FROM profile_context_packs
             WHERE application_id IS NULL AND message_thread_id IS NULL
               AND purpose = ANY(%s);""",
        (sorted(required),),
    )
    missing = sorted(required - {str(row[0]) for row in cur.fetchall()})
    if missing:
        blockers.append("missing base context packs: " + ", ".join(missing))
    return blockers


def soft_block_missing_profile(cur, *, application_id: str, step: str) -> bool:
    blockers = profile_prerequisite_blockers(cur)
    if not blockers:
        return False
    detail = "; ".join(blockers)
    cur.execute(
        """INSERT INTO pipeline_events(application_id,from_step,to_step,actor,reason,detail_json)
           VALUES (%s,%s,%s,'pipeline-preflight',%s,%s);""",
        (application_id, step, step,
         "Missing profile prerequisites; state preserved for recovery instead of transitioning to error.",
         Jsonb({"blockers": blockers, "browser_io": False, "fabrication": False})),
    )
    print(f"    blocked by profile prerequisites (state preserved): {detail}")
    return True


# ---------------------------------------------------------------- fit review gate

def create_fit_review_approval(
    cur, application_id: str, *, company: str, job_title: str, score: int,
) -> None:
    """Direct insert, mirroring approval_service_v1.py's cmd_create. Not
    shelled out to, because this runs inside the same transaction as the
    transition it gates -- a subprocess call would need its own connection
    and could race the commit."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    summary = f"fit_review: {company} / {job_title} (score {score}, borderline)"

    cur.execute(
        """
        INSERT INTO approval_requests
          (type, application_id, payload_json, status, approval_channel,
           approval_token_hash, token_expires_at, requested_by, summary_text,
           max_attempts, created_at)
        VALUES ('fit_review', %s, %s, 'pending', 'cli', %s,
                now() + make_interval(hours => %s), 'orchestrator', %s, 5, now());
        """,
        (application_id, Jsonb({"score": score, "reason": "borderline_fit_60_75"}),
         token_hash, FIT_REVIEW_TTL_HOURS, summary),
    )
    print(f"    fit review needed (score {score}/100). Decide with:")
    print(f"      python services/approval/approval_service_v1.py approve --token {token}")
    print(f"      python services/approval/approval_service_v1.py deny    --token {token}")
    print(f"    (expires in {FIT_REVIEW_TTL_HOURS}h)")


def resolve_fit_review(cur, application_id: str, *, apply: bool) -> None:
    """Consume an already-redeemed fit_review approval. The human decision
    was already made (and cryptographically proven) at token-redemption
    time by approval_service_v1.py; this only reflects that decision into
    the state machine. Same trust boundary as the L7 submit gate."""
    cur.execute(
        """
        SELECT status FROM approval_requests
        WHERE application_id = %s AND type = 'fit_review'
        ORDER BY created_at DESC LIMIT 1;
        """,
        (application_id,),
    )
    row = cur.fetchone()
    if not row or row[0] == "pending":
        print("    awaiting the user's fit-review decision "
              "(approval_service_v1.py approve/deny)")
        return
    if not apply:
        print(f"    (dry run: would resolve fit review as {row[0]})")
        return
    if row[0] == "approved":
        transition(cur, application_id=application_id, to_step="fit_analyzed",
                   actor="human", reason="User approved the borderline fit review.")
    else:
        transition(cur, application_id=application_id, to_step="fit_rejected",
                   actor="human", reason=f"User {row[0]} the borderline fit review.")


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

    # awaiting_fit_review is requires_human=true (a human must decide), but
    # unlike the other human gates that need a real side-effecting action
    # (submit, send), the human's decision already happened the moment they
    # redeemed the approval token. This step's only job is to reflect that
    # already-authorized decision into current_step, so it is special-cased
    # ahead of the generic requires_human early-return below.
    if step == "awaiting_fit_review":
        resolve_fit_review(cur, application_id, apply=apply)
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
        if soft_block_missing_profile(cur, application_id=application_id, step=step):
            return
        # Cost gate: the fit-analysis call below is the first LLM spend for
        # this job. Refusing here (rather than after the call) is the whole
        # point of a *pre*-spend gate. A budget block is treated as
        # transient: the application just waits at 'screened' until the
        # daily budget resets or is raised, rather than erroring out.
        cost_ok, cost_out = check_cost_budget("full_pipeline", application_id=application_id)
        if not cost_ok:
            tail = cost_out.strip().splitlines()[-1] if cost_out.strip() else "budget refused"
            print(f"    cost gate: {tail}")
            record_failure(cur, application_id, step, cost_out, transient=True)
            return

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

        # Preserve market observations even when this application is rejected.
        # The DB trigger queues every intake before filtering; this call drains
        # the same LLM/evidence-grounded queue for this application when it
        # reaches fit analysis. It never changes the fit decision.
        mok, mout, _ = run_step(
            MARKET_INTELLIGENCE_SCRIPT,
            ["process", "--application-id", application_id, "--apply"],
        )
        if not mok:
            tail = mout.strip().splitlines()[-1] if mout.strip() else "unavailable"
            print(f"    market intelligence: skipped (non-fatal) -- {tail[:160]}")

        if decision == "reject":
            transition(cur, application_id=application_id, to_step="fit_rejected",
                       actor="orchestrator", reason=f"Fit {score} / {decision}")
        elif decision == "ask_user":
            # Previously ask_user and approve_research were treated
            # identically and both sailed straight through -- the 60-75
            # "ask the user first" tier was declared but never enforced.
            create_fit_review_approval(
                cur, application_id, company=company, job_title=job_title, score=score,
            )
            transition(cur, application_id=application_id, to_step="awaiting_fit_review",
                       actor="orchestrator", reason=f"Borderline fit {score}; asking user.")
        else:
            transition(cur, application_id=application_id, to_step="fit_analyzed",
                       actor="orchestrator", reason=f"Fit {score} / {decision}")

    elif step == "fit_analyzed":
        # L5 Research Router: previously never invoked at all -- the
        # orchestrator jumped straight from fit_analyzed to docs_generated
        # and company_research_v1.py sat completely unreferenced. Research
        # is best-effort here: a company with no fetchable web presence, or
        # OpenClaw being unavailable, must not block document generation.
        rok, rout, _ = run_module(RESEARCH_MODULE,
                                  ["--for-application", application_id, "--apply"])
        if rok:
            print("    company research: refreshed")
        else:
            tail = rout.strip().splitlines()[-1] if rout.strip() else "unavailable"
            print(f"    company research: skipped (non-fatal) -- {tail[:160]}")
            cur.execute(
                """
                INSERT INTO pipeline_events
                  (application_id, from_step, to_step, actor, reason, detail_json)
                VALUES (%s, %s, %s, 'company_research_router', %s, %s);
                """,
                (application_id, step, step,
                 "Research skipped or failed; non-fatal, proceeding to doc-gen.",
                 Jsonb({"output": rout[-1500:]})),
            )

        ok, out, transient = run_step(DOCGEN_SCRIPT,
                                      ["--application-id", application_id,
                                       "--doc-type", "resume", "--apply"])
        if not ok:
            record_failure(cur, application_id, step, out, transient=transient)
            return

        # Cover letter is generated alongside the resume, best-effort: a
        # cover-letter failure must not block the resume (already saved)
        # from reaching QA. short_answers is deliberately NOT auto-generated
        # here -- it requires real --question text captured from the
        # application form itself (see generate_documents_v1.py), which
        # only exists once L3/L7 has actually opened the form. It stays a
        # manually-triggered CLI step for that reason, not an oversight.
        cok, cout, _ = run_step(DOCGEN_SCRIPT,
                                ["--application-id", application_id,
                                 "--doc-type", "cover_letter", "--apply"])
        if cok:
            print("    cover letter: drafted")
        else:
            tail = cout.strip().splitlines()[-1] if cout.strip() else "unknown error"
            print(f"    cover letter: failed (non-fatal, resume proceeds) -- {tail[:160]}")

        transition(cur, application_id=application_id, to_step="docs_generated",
                   actor="orchestrator",
                   reason="Resume (and cover letter, best-effort) drafts generated.")



    elif step == "docs_generated":
        cur.execute(
            """SELECT id::text FROM generated_documents
                 WHERE application_id = %s AND doc_type = 'resume'
                 ORDER BY version DESC, created_at DESC LIMIT 1;""",
            (application_id,),
        )
        resume_row = cur.fetchone()
        if not resume_row:
            record_failure(cur, application_id, step, "Resume draft was not found for this application.", transient=True)
            return
        ok, out, transient = run_step(VERIFY_SCRIPT, ["--document-id", resume_row[0], "--apply"])
        if not ok and transient:
            record_failure(cur, application_id, step, out, transient=True)
            return
        # A non-zero exit with qa_status='fail' is a real verdict, not a crash,
        # so only transient failures short-circuit here.

        # Cover letters have their own grounding/positioning verifier.  Keep
        # this lane independent from resume QA, but do not leave a generated
        # cover letter permanently unreviewed when it exists.
        cur.execute(
            """SELECT id::text FROM generated_documents
                 WHERE application_id = %s AND doc_type = 'cover_letter'
                 ORDER BY version DESC, created_at DESC LIMIT 1;""",
            (application_id,),
        )
        cover_row = cur.fetchone()
        if cover_row:
            cok, cout, ctransient = run_step(VERIFY_SCRIPT, ["--document-id", cover_row[0], "--apply"])
            if not cok and ctransient:
                # Cover letter is supplemental. A transient verifier outage may
                # leave it unavailable for this application, but must not hold
                # the primary reviewed resume at docs_generated.
                cur.execute(
                    """INSERT INTO pipeline_events(application_id,from_step,to_step,actor,reason,detail_json)
                       VALUES (%s,%s,%s,'cover-letter-verifier',%s,%s);""",
                    (application_id, step, step,
                     "Supplemental cover-letter verification deferred; primary resume proceeds.",
                     Jsonb({"output": cout[-1500:]})),
                )
                print("    cover letter QA: transient failure; omitted/deferred without blocking resume")

        cur.execute(
            """
            SELECT id::text, qa_status, revision_round
            FROM generated_documents
            WHERE application_id = %s AND doc_type = 'resume'
            ORDER BY version DESC, created_at DESC
            LIMIT 1;
            """,
            (application_id,),
        )
        r = cur.fetchone()
        doc_id, qa, rround = (r[0], r[1], r[2]) if r else (None, None, 0)

        if qa == "pass":
            transition(cur, application_id=application_id, to_step="docs_verified",
                       actor="truth_quality_checker", reason="All claims supported.")
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


# ---------------------------------------------------------------- durable orchestration claim

def claim_application(cur, application_id: str) -> tuple[str, str] | None:
    run_id = str(uuid.uuid4())
    cur.execute(
        """UPDATE applications a
              SET processing_run_id=%s::uuid, processing_step=a.current_step,
                  processing_started_at=now(),
                  processing_lease_expires_at=now()+make_interval(secs => %s)
            FROM pipeline_steps ps
           WHERE a.id=%s AND ps.step=a.current_step
             AND ps.is_terminal=false AND ps.requires_human=false
             AND (a.processing_run_id IS NULL OR a.processing_lease_expires_at <= now())
        RETURNING a.current_step;""",
        (run_id, ORCHESTRATOR_LEASE_SECONDS, application_id),
    )
    row = cur.fetchone()
    return (run_id, str(row[0])) if row else None


def release_application_claim(cur, application_id: str, run_id: str) -> None:
    cur.execute(
        """UPDATE applications
              SET processing_run_id=NULL, processing_step=NULL, processing_started_at=NULL,
                  processing_lease_expires_at=NULL
            WHERE id=%s AND processing_run_id=%s::uuid;""",
        (application_id, run_id),
    )


def _subprocess_env(args: List[str]) -> dict[str, str]:
    env = os.environ.copy()
    if "--application-id" in args:
        try:
            env["JOBOS_APPLICATION_ID"] = str(args[args.index("--application-id") + 1])
        except (ValueError, IndexError):
            pass
    return env

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
    global _ACTIVE_PROCESSING_RUN_ID
    with conn.cursor() as cur:
        if args.application_id:
            ids = [args.application_id]
        else:
            cur.execute(
                """
                SELECT a.id::text FROM applications a
                JOIN pipeline_steps ps ON ps.step = a.current_step
                WHERE ps.is_terminal = false AND ps.requires_human = false
                  AND (a.processing_run_id IS NULL OR a.processing_lease_expires_at <= now())
                ORDER BY ps.sort_order, a.updated_at;
                """
            )
            ids = [r[0] for r in cur.fetchall()]

        if not ids:
            print("Nothing to advance.")
            return 0

        for app_id in ids:
            run_id: str | None = None
            try:
                if args.apply:
                    claimed = claim_application(cur, app_id)
                    if not claimed:
                        conn.rollback()
                        print(f"  {app_id}: already claimed, terminal, or waiting on a human; skipped")
                        continue
                    run_id, _claimed_step = claimed
                    conn.commit()  # claim is durable before any long external work
                    _ACTIVE_PROCESSING_RUN_ID = run_id
                advance_one(cur, app_id, apply=args.apply)
                if args.apply and run_id:
                    release_application_claim(cur, app_id, run_id)
                    conn.commit()
            except Exception as e:
                conn.rollback()
                if args.apply and run_id:
                    try:
                        release_application_claim(cur, app_id, run_id)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                print(f"    error: {type(e).__name__}: {e}")
            finally:
                _ACTIVE_PROCESSING_RUN_ID = None

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

    with psycopg.connect(database_dsn(), autocommit=False) as conn:
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
