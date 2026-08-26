from __future__ import annotations

import base64
import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


class SavepointCursor:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))


def test_soft_fail_db_section_uses_savepoint_and_returns_nan():
    from services.review import approval_context_v1 as ctx

    cur = SavepointCursor()

    def broken():
        raise RuntimeError("optional source unavailable")

    assert ctx._safe_db(cur, broken) == "NaN"
    joined = "\n".join(cur.sql)
    assert "SAVEPOINT jobos_context_softfail" in joined
    assert "ROLLBACK TO SAVEPOINT jobos_context_softfail" in joined
    assert "RELEASE SAVEPOINT jobos_context_softfail" in joined


def test_context_diff_ignores_delivery_noise_but_reports_material_changes():
    from services.review.approval_context_v1 import context_diff

    before = {
        "approval": {"approval_request_id": "old", "expires_at": "old-time"},
        "job": {"job_title": "Engineer"},
        "form": {"proposed_fields": [{"field": "phone", "value": "111"}]},
    }
    after = {
        "approval": {"approval_request_id": "new", "expires_at": "new-time"},
        "job": {"job_title": "Engineer"},
        "form": {"proposed_fields": [{"field": "phone", "value": "222"}]},
    }
    diff = context_diff(before, after)
    paths = {item["path"] for item in diff["changed"]}
    assert "approval.approval_request_id" not in paths
    assert "approval.expires_at" not in paths
    assert "form.proposed_fields" in paths


def test_gmail_search_is_bounded_and_explicitly_checks_spam(monkeypatch):
    from services.auth import gmail_verification_v1 as gmail

    calls: list[list[str]] = []

    def fake_run(args, timeout=45):
        calls.append(list(args))
        q = args[3]
        if "in:spam" in q and "-in:spam" not in q:
            return [{"id": "spam-1", "subject": "Verification code", "from": "recruiting@example.com"}]
        return [{"id": "inbox-1", "subject": "Verification code", "from": "recruiting@example.com"}]

    monkeypatch.setattr(gmail, "_run_gog", fake_run)
    ids = gmail.search_candidate_ids(
        recipient="candidate@example.com",
        requested_at=datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc),
        max_results=7,
    )
    assert ids == ["inbox-1", "spam-1"]
    queries = [call[3] for call in calls]
    assert any("-in:spam" in q for q in queries)
    assert any(" in:spam" in q for q in queries)
    assert all("to:candidate@example.com" in q for q in queries)
    assert all(call[-2:] == ["--max", "7"] for call in calls)


def test_gmail_numeric_otp_and_magic_link_are_hashed_not_returned_in_candidate(monkeypatch):
    from services.auth import gmail_verification_v1 as gmail

    code = "481293"
    link = "https://accounts.example.com/verify?token=secret-token"

    requested_at = datetime.now(timezone.utc)
    received_ms = int((requested_at.timestamp() + 1) * 1000)
    monkeypatch.setattr(gmail, "search_candidate_ids", lambda **kwargs: ["m1"])
    monkeypatch.setattr(gmail, "read_message", lambda message_id, sanitized: {
        "id": "m1", "subject": "Verification code", "from": "recruiting@example.com",
        "body": f"Your verification code is {code}", "internalDate": str(received_ms),
    })
    candidate = gmail.discover_verification(
        recipient="candidate@example.com", requested_at=requested_at,
        employer_origin="https://example.com", max_results=3,
    )
    assert candidate["kind"] == "numeric_code"
    assert candidate["secret_sha256"] == hashlib.sha256(code.encode()).hexdigest()
    assert code not in json.dumps(candidate, default=str)

    monkeypatch.setattr(gmail, "read_message", lambda message_id, sanitized: (
        {"id": "m1", "subject": "Verify your email", "from": "recruiting@example.com", "body": "Verify your email", "internalDate": str(received_ms)}
        if sanitized else
        {"id": "m1", "subject": "Verify your email", "from": "recruiting@example.com", "body": f"Verify here: {link}", "internalDate": str(received_ms)}
    ))
    candidate = gmail.discover_verification(
        recipient="candidate@example.com", requested_at=requested_at,
        employer_origin="https://example.com", max_results=3,
    )
    assert candidate["kind"] == "magic_link"
    assert candidate["secret_sha256"] == hashlib.sha256(link.encode()).hexdigest()
    assert link not in json.dumps(candidate, default=str)


class VaultCursor:
    def __init__(self):
        self.rows: list[dict] = []
        self._one = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        compact = " ".join(str(sql).split())
        params = params or ()
        self.rowcount = 0
        if compact.startswith("UPDATE credential_vault_entries SET status = 'rotated'"):
            origin, account, kind = params
            for row in self.rows:
                if (row["origin"], row["account"], row["kind"], row["status"]) == (origin, account, kind, "active"):
                    row["status"] = "rotated"; self.rowcount += 1
            self._one = None
        elif compact.startswith("INSERT INTO credential_vault_entries"):
            origin, account, kind, ciphertext, nonce, aad, digest, key_version, metadata = params
            ident = f"v{len(self.rows)+1}"
            self.rows.append({"id": ident, "origin": origin, "account": account, "kind": kind,
                              "ciphertext": bytes(ciphertext), "nonce": bytes(nonce), "aad": aad,
                              "digest": digest, "key_version": key_version, "metadata": metadata, "status": "active"})
            self._one = (ident,)
        elif compact.startswith("SELECT ciphertext, nonce, aad, secret_sha256"):
            origin, account, kind = params
            match = next((r for r in reversed(self.rows)
                          if (r["origin"], r["account"], r["kind"], r["status"]) == (origin, account, kind, "active")), None)
            self._one = (match["ciphertext"], match["nonce"], match["aad"], match["digest"]) if match else None
        else:
            raise AssertionError(compact)

    def fetchone(self):
        return self._one


def test_vault_encrypts_and_rotation_keeps_only_latest_active(monkeypatch):
    from services.security import credential_vault_v1 as vault

    key = bytes(range(32))
    monkeypatch.setattr(vault, "load_master_key", lambda: key)
    cur = VaultCursor()
    vault.store_secret(cur, origin="https://careers.example.com", account_key="Me@Example.com",
                       secret_kind="password", secret="first-secret")
    assert vault.read_secret(cur, origin="https://careers.example.com", account_key="me@example.com",
                             secret_kind="password") == "first-secret"
    assert b"first-secret" not in cur.rows[0]["ciphertext"]
    vault.store_secret(cur, origin="https://careers.example.com", account_key="me@example.com",
                       secret_kind="password", secret="second-secret")
    assert [r["status"] for r in cur.rows] == ["rotated", "active"]
    assert vault.read_secret(cur, origin="https://careers.example.com", account_key="me@example.com",
                             secret_kind="password") == "second-secret"


def test_checkpoint_email_otp_and_other_mfa_are_distinct_states():
    from services.application_actions.privileged_action_v1 import detect_page_state

    def snap(text):
        return {"snapshot": text}

    assert detect_page_state("https://accounts.example.com", snap("Verify you are human CAPTCHA"), [])[0] == "needs_human_checkpoint"
    assert detect_page_state("https://accounts.example.com", snap("Check your email for a verification code"), [])[0] == "needs_email_verification"
    assert detect_page_state("https://accounts.example.com", snap("Approve the sign-in in your authenticator"), [])[0] == "needs_mfa"


def test_linkedin_easy_apply_is_separate_platform_and_submit_is_privileged_only():
    from services.application_actions import action_request_v1 as req
    from services.application_actions.privileged_action_v1 import detect_platform, SUBMIT_LABELS
    from services.autofill import autofill_agent_v1 as legacy

    assert detect_platform("https://www.linkedin.com/jobs/view/123", {"snapshot": "dialog Easy Apply Contact info"}) == "linkedin_easy_apply"
    assert "privileged_submit_application" in req.PRIVILEGED_TYPES
    assert "privileged_advance_application_step" in req.PRIVILEGED_TYPES
    # The compatibility autofill CLI remains plan-only and contains no submit browser primitive.
    assert not hasattr(legacy, "act_submit")
    assert "submit application" in SUBMIT_LABELS


def test_telegram_softfail_message_keeps_nan_and_privileged_submit_button():
    from services.telegram import telegram_review_bot_v1 as tg

    row = ("review1", "approval_request", "urgent", "Final approval", "Review before submit",
           "Example Co", "Engineer", {"approval_type": "privileged_submit_application"})
    envelope = {"job": "NaN", "fit": "NaN", "approval": "NaN", "browser": "NaN",
                "documents": "NaN", "form": "NaN", "auth": "NaN"}
    text = tg._message_text(row, envelope, {"baseline": True, "changed": []})
    assert "NaN" in text
    assert "soft-fail" in text

    cur = SavepointCursor()
    keyboard = tg._keyboard(cur, "review1", 123, "approval_request",
                            {"approval_type": "privileged_submit_application"})
    assert "APPROVE SUBMIT" in keyboard


def test_migration_071_contracts_and_visible_checkpoint_note_only_in_privileged_executor():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "db/migrations/071_human_approval_bus_and_privileged_actions.sql").read_text()
    executor = (root / "services/application_actions/privileged_action_v1.py").read_text()
    worker = (root / "services/browser-controller/browser_queue_worker.py").read_text()
    assert "credential_vault_entries" in sql
    assert "approval_context_snapshots" in sql
    assert "privileged_action_executions" in sql
    assert "linkedin_easy_apply" in sql
    assert "application_form_ready', 'awaiting_approval" in sql
    assert "HUMAN CHECKPOINT BOUNDARY — DO NOT MERGE WITH OTP/MFA APPROVAL" in executor
    # New feature implementation does not add the new checkpoint marker to the frozen browser worker.
    assert "HUMAN CHECKPOINT BOUNDARY — DO NOT MERGE WITH OTP/MFA APPROVAL" not in worker


def test_profile_optional_query_uses_savepoint_and_create_account_can_seed_vault():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services/application_actions/privileged_action_v1.py").read_text()
    assert 'SAVEPOINT jobos_profile_values' in source
    assert 'ROLLBACK TO SAVEPOINT jobos_profile_values' in source
    assert 'store_secret(cur, origin=vault_origin' in source
    assert 'generated_by": "jobos-account-registration"' in source
    assert 'employer password is not available in the encrypted vault' in source


def test_magic_link_requires_separate_trusted_domain_before_opening():
    root = Path(__file__).resolve().parents[1]
    watcher = (root / "services/auth/gmail_verification_watcher_v1.py").read_text()
    executor = (root / "services/application_actions/privileged_action_v1.py").read_text()
    assert 'trust_source": "gmail_magic_link"' in watcher
    assert 'Trust email-verification link domain' in watcher
    assert 'payload.get("trust_source") == "gmail_magic_link"' in executor
    assert 'email magic-link domain does not match the approved trust gate' in executor
    assert '_require_trusted_target(cur, secret, application_id=app_id, purpose="gmail_magic_link")' in executor
    assert 'application_scoped_domain_trusts' in executor


def test_openclaw_gmail_hook_wake_bridge_is_additive_and_payload_minimal():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "bootstrap/openclaw/openclaw.template.json").read_text())
    gmail_mapping = next(m for m in config["hooks"]["mappings"] if m.get("match", {}).get("path") == "gmail")
    assert gmail_mapping["action"] == "agent"
    assert gmail_mapping["deliver"] is True
    assert gmail_mapping["transform"] == {"module": "jobos-gmail-wake.mjs", "export": "transformGmail"}
    transform = (root / "bootstrap/openclaw/hooks/transforms/jobos-gmail-wake.mjs").read_text()
    assert "message_id" in transform
    assert "ctx?.payload?.messages?.[0]?.id" in transform
    assert "snippet" not in transform.casefold()
    assert "messages?.[0]?.body" not in transform.casefold()
    assert "messages?.[0]?.snippet" not in transform.casefold()
    assert "return {};" in transform
    watcher = (root / "services/auth/gmail_verification_watcher_v1.py").read_text()
    assert "hook payload is untrusted wake data" in watcher
    assert "--wake-listen" in watcher


def test_privileged_post_io_observable_change_and_consent_helpers():
    from services.application_actions.privileged_action_v1 import (
        _consent_effect_verified, _observable_page_change, _snapshot_text_sha256,
    )

    before = {"snapshot": "- heading Job\n- button Apply"}
    unchanged = {"target_id": "t1", "url": "https://jobs.example/app",
                 "snapshot_sha256": _snapshot_text_sha256(before)}
    changed_snapshot = {"target_id": "t1", "url": "https://jobs.example/app",
                        "snapshot_sha256": _snapshot_text_sha256({"snapshot": "- heading Job\n- dialog Easy Apply"})}
    changed_url = {"target_id": "t1", "url": "https://jobs.example/app/step-2",
                   "snapshot_sha256": _snapshot_text_sha256(before)}
    changed_target = {"target_id": "t2", "url": "https://jobs.example/app",
                      "snapshot_sha256": _snapshot_text_sha256(before)}
    assert not _observable_page_change(before_target="t1", before_url="https://jobs.example/app",
                                       before_snapshot=before, after=unchanged)
    assert _observable_page_change(before_target="t1", before_url="https://jobs.example/app",
                                   before_snapshot=before, after=changed_snapshot)
    assert _observable_page_change(before_target="t1", before_url="https://jobs.example/app",
                                   before_snapshot=before, after=changed_url)
    assert _observable_page_change(before_target="t1", before_url="https://jobs.example/app",
                                   before_snapshot=before, after=changed_target)

    approved = [{"ref": "c1", "label": "I agree to terms", "selected": False}]
    assert _consent_effect_verified(approved, [{"ref": "c1", "label": "I agree to terms", "selected": True}],
                                    page_changed=False)
    assert not _consent_effect_verified(approved, [{"ref": "c1", "label": "I agree to terms", "selected": False}],
                                        page_changed=True)
    assert not _consent_effect_verified(approved, [], page_changed=True)
    assert not _consent_effect_verified(approved, [], page_changed=False)


def test_privileged_executor_fences_unverified_post_io_effects():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services/application_actions/privileged_action_v1.py").read_text()
    execution = source[source.index("def execute_one"):source.index("def recover_stale_executions")]
    assert "Apply handoff click produced no observable navigation or modal change" in execution
    assert "employer account action produced no observable browser change" in execution
    assert "application wizard step click produced no observable page change" in execution
    assert "approved consent controls were not observably accepted after browser I/O" in execution
    assert "email verification browser I/O produced no observable page change" in execution
    assert 'state = "needs_reconciliation" if io_started else "failed"' in execution
