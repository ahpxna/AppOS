from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"


def _sql(version: int) -> str:
    matches = sorted(MIGRATIONS.glob(f"{version:03d}_*.sql"))
    assert len(matches) == 1, f"expected exactly one migration {version:03d}, got {matches}"
    return matches[0].read_text(encoding="utf-8")


def test_db_authority_migrations_087_through_096_exist_once_and_are_transaction_wrapped():
    for version in range(87, 97):
        text = _sql(version)
        assert re.search(r"\bBEGIN\s*;", text, re.I)
        assert re.search(r"\bCOMMIT\s*;\s*$", text, re.I)


def test_pipeline_authority_keeps_audit_events_compatible_without_forging_state_changes():
    text = _sql(88)
    assert "CREATE OR REPLACE FUNCTION jobos_prepare_pipeline_event()" in text
    assert "state-changing pipeline events must use jobos_transition_application()" in text
    assert "NEW.pipeline_version := coalesce(NEW.pipeline_version, v_current_version)" in text
    assert "SELECT coalesce(max(sequence_no),0)+1 INTO v_sequence_no" in text
    assert "v_sequence_no,v_new_version,v_kind" in text
    # Backfill must not count NULL->intake or same-step audit notes as state versions.
    assert "WHEN from_step IS NOT NULL AND to_step IS DISTINCT FROM from_step THEN 1" in text


def test_post_v4_llm_hardening_is_preserved_while_db_call_ledger_is_added():
    source = (ROOT / "services" / "common" / "llm_gateway.py").read_text(encoding="utf-8")
    assert "import hashlib" in source
    assert "import math" in source
    assert "def _sha_payload" in source
    assert "request_sha256" in source
    assert "math.isfinite" in source


def test_post_v4_telegram_shape_hardening_is_preserved_with_durable_inbox():
    source = (ROOT / "services" / "telegram" / "telegram_review_bot_v1.py").read_text(encoding="utf-8")
    poll = source.split("def poll_once(", 1)[1].split("\ndef main()", 1)[0]
    assert "updates = _result_list(payload)" in poll
    assert "update_id = _safe_int(update.get(\"update_id\"))" in poll
    assert "isinstance(callback, dict)" in poll
    assert "isinstance(message, dict)" in poll
    assert "INSERT INTO telegram_updates" in poll
    assert "_reap_stale_transport(cur)" in poll
