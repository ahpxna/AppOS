from pathlib import Path
import sys
import types

import pytest

try:
    from psycopg.types.json import Jsonb as _Jsonb  # noqa: F401
except ModuleNotFoundError:
    psycopg = types.ModuleType("psycopg")
    psycopg_types = types.ModuleType("psycopg.types")
    psycopg_json = types.ModuleType("psycopg.types.json")
    psycopg_json.Jsonb = lambda value: value
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.types", psycopg_types)
    sys.modules.setdefault("psycopg.types.json", psycopg_json)

from services.discovery.linkedin_discovery_v1 import LinkedInDiscoveryError, normalize_jobs, validate_saved_request
from services.autofill.autofill_executor_v1 import OpenClawTransport


def test_saved_request_is_bounded():
    assert validate_saved_request(1)["max_results"] == 1
    assert validate_saved_request(20)["max_results"] == 20
    with pytest.raises(LinkedInDiscoveryError):
        validate_saved_request(21)


def test_saved_jobs_use_same_strict_canonical_job_evidence_boundary():
    response = {"jobs": [{"company": "Example", "title": "Security Engineer",
                           "location": "New Jersey", "work_mode": "hybrid",
                           "url": "https://www.linkedin.com/jobs/view/123456789/?trackingId=ignored",
                           "jd_text": "A" * 250}]}
    rows = normalize_jobs(response, 10)
    assert rows[0]["url"] == "https://www.linkedin.com/jobs/view/123456789/"
    assert len(rows[0]["jd_text"]) == 250


def test_screenshot_path_extraction_requires_existing_file(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    assert OpenClawTransport._find_media_path({"data": {"path": str(image)}}) == image.resolve()
    assert OpenClawTransport._find_media_path({"path": str(tmp_path / "missing.png")}) is None



def test_linkedin_search_discovery_has_fail_closed_blocker_boundary():
    worker = (Path(__file__).resolve().parents[1] / "services" / "browser-controller" / "browser_queue_worker.py").read_text(encoding="utf-8")
    start = worker.index("def handle_discover_linkedin_jobs")
    end = worker.index("def handle_discover_linkedin_saved_jobs", start)
    handler = worker[start:end]
    assert "execute_parallel_bypass" not in handler
    assert "_fake_mouse_routine" not in handler
    assert "raise PermanentTaskError" in handler
    for marker in ("captcha", "verification", "security check", "checkpoint", "login required"):
        assert marker in handler.casefold()


def test_review_revision_gate_and_jd_binding_are_persistent():
    review = (Path(__file__).resolve().parents[1] / "services" / "review" / "review_service_v1.py").read_text(encoding="utf-8")
    assert 'payload.get("human_revision_required")' in review
    assert '"human_revision_required": True' in review
    assert "gd.source_jd_hash, a.jd_hash" in review
    assert "hra.review_item_id = %s" in review
    assert "gda.application_id = %s" in review


def test_reconciliation_close_leaves_no_stuck_needs_reconciliation_state():
    source = (Path(__file__).resolve().parents[1] / "services" / "autofill" / "autofill_reconcile_v1.py").read_text(encoding="utf-8")
    assert "execution_state = 'partial'" in source
    assert "status = 'reconciled'" in source
    assert "execution_state = 'needs_reconciliation'" in source


def test_watchdog_requeues_only_provably_pre_io_tasks():
    source = (Path(__file__).resolve().parents[1] / "services" / "browser-controller" / "watchdog.py").read_text(encoding="utf-8")
    assert "execution_state = 'not_started'" in source
    assert "<> 'executing'" not in source
    assert "mark_uncertain_expired_tasks" in source
    assert "execution_state IN ('executing', 'partial', 'completed', 'needs_reconciliation')" in source


def test_worker_retry_policy_never_blindly_replays_journaled_autofill():
    source = (Path(__file__).resolve().parents[1] / "services" / "browser-controller" / "browser_queue_worker.py").read_text(encoding="utf-8")
    start = source.index("def requeue_or_fail")
    end = source.index("# ---------------------------------------------------------------- guards", start)
    retry_logic = source[start:end]
    assert "autofill_action_journal" in retry_logic
    assert "execution_state = 'needs_reconciliation'" in retry_logic
    assert "retry_count <= max_retries" in source
    assert "Autofill retries exhausted before browser I/O" in retry_logic


def test_approval_create_expires_ttl_stale_idempotency_rows_first():
    source = (Path(__file__).resolve().parents[1] / "services" / "approval" / "approval_service_v1.py").read_text(encoding="utf-8")
    start = source.index("def cmd_create")
    end = source.index("def cmd_decide", start) if "def cmd_decide" in source[start:] else start + 9000
    create_logic = source[start:end]
    expiry = create_logic.index("token_expires_at <= now()")
    lookup = create_logic.index("WHERE idempotency_key = %s")
    assert expiry < lookup


def test_worker_dead_letter_archive_matches_live_task_status():
    source = (Path(__file__).resolve().parents[1] / "services" / "browser-controller" / "browser_queue_worker.py").read_text(encoding="utf-8")
    start = source.index("def dead_letter_exhausted")
    end = source.index("def claim_next_task", start)
    logic = source[start:end]
    assert "INSERT INTO dead_letter_tasks" in logic
    assert "SET status = 'dead_letter'" in logic


def test_stale_approval_can_be_denied_without_reauthorizing_stale_binding():
    source = (Path(__file__).resolve().parents[1] / "services" / "approval" / "approval_service_v1.py").read_text(encoding="utf-8")
    redeem_start = source.index("def redeem")
    redeem_end = source.index("def decide_request_by_id", redeem_start)
    redeem_logic = source[redeem_start:redeem_end]
    assert 'if decision == "approve":' in redeem_logic
    decide_start = redeem_end
    decide_end = source.index("# ---------------------------------------------------------------- list / expire", decide_start)
    decide_logic = source[decide_start:decide_end]
    assert 'if normalized == "approve":' in decide_logic


def test_reconciliation_review_cannot_be_dismissed_while_browser_state_is_uncertain():
    source = (Path(__file__).resolve().parents[1] / "services" / "review" / "review_service_v1.py").read_text(encoding="utf-8")
    start = source.index('elif item_type == "reconciliation_required"')
    end = source.index('elif item_type == "application_ready"', start)
    logic = source[start:end]
    assert 'task[0] == "needs_reconciliation"' in logic
    assert "Reject/revise cannot dismiss" in logic
