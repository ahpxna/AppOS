from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"


def _sql(version: int) -> str:
    matches = sorted(MIGRATIONS.glob(f"{version:03d}_*.sql"))
    assert len(matches) == 1, f"expected exactly one migration {version:03d}, got {matches}"
    return matches[0].read_text(encoding="utf-8")


def test_db_authority_migrations_087_through_097_exist_once_and_are_transaction_wrapped():
    for version in range(87, 98):
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


def test_approval_event_binding_lookup_has_explicit_uuid_type_and_no_ambiguous_case_parameter():
    source = (ROOT / "services" / "approval" / "approval_service_v1.py").read_text(encoding="utf-8")
    block = source.split("def log_event(", 1)[1].split("\n\n# ---------------------------------------------------------------- autofill parent/child gating", 1)[0]
    assert "WHERE id=%s::uuid" in block
    assert "CASE WHEN %s IS NULL" not in block


def test_autofill_db_integration_fixture_seeds_and_binds_first_class_plan():
    source = (ROOT / "tests" / "integration" / "test_autofill_execution_lifecycle.py").read_text(encoding="utf-8")
    record = source.split("def _record(", 1)[1].split("\n\nclass FakeTransport", 1)[0]
    assert "_insert_first_class_autofill_plan(" in record
    assert "bound_autofill_plan_id" in record
    assert "bound_autofill_plan_key" in record
    assert "bound_pipeline_version" in record
    assert "autofill_plan_id, lease_expires_at" in record


def test_autofill_integration_fixture_binds_all_trigger_required_typed_fields():
    source = (ROOT / "tests" / "integration" / "test_autofill_execution_lifecycle.py").read_text(encoding="utf-8")
    record = source.split("def _record(", 1)[1].split("\n\nclass FakeTransport", 1)[0]
    assert "expected_target_id" in record
    assert "bound_document_id" in record
    assert "bound_document_sha256" in record
    assert "expected_origin" in record
    assert "expected_initial_url" in record
    assert "expected_page_fingerprint" in record
    assert "bound_autofill_input_hash" in record
    assert "bound_pipeline_version" in record
    assert "bound_autofill_plan_key" in record
    assert "bound_autofill_plan_id" in record


def test_root_db_autofill_lifecycle_uses_schema_096_first_class_plan_binding():
    source = (ROOT / "test_autofill_execution_db_integration.py").read_text(encoding="utf-8")
    assert "096_db_authority_final_invariants.sql" in source
    assert "INSERT INTO autofill_plans" in source
    assert "bound_autofill_plan_id" in source
    assert "bound_autofill_plan_key" in source
    assert "bound_pipeline_version" in source
    assert "expected_target_id" in source
    assert "autofill_plan_id" in source


def test_delegated_parent_fixtures_use_real_document_artifact_and_full_typed_binding():
    source = (ROOT / "tests" / "integration" / "test_autofill_execution_lifecycle.py").read_text(encoding="utf-8")
    for function_name, next_marker in (
        ("test_missing_delegated_child_is_repaired_and_bound_to_exact_parent", "test_same_plan_new_parent_gets_distinct_upload_child"),
        ("test_same_plan_new_parent_gets_distinct_upload_child", "_configure_delegated_gate"),
    ):
        block = source.split(f"def {function_name}(", 1)[1].split(f"\ndef {next_marker}(", 1)[0]
        assert "_insert_fixture_document_artifact(" in block
        for required in (
            "bound_document_id", "bound_document_sha256", "expected_target_id", "expected_origin",
            "expected_initial_url", "expected_page_fingerprint", "bound_autofill_input_hash",
            "bound_pipeline_version", "bound_autofill_plan_key", "bound_autofill_plan_id",
        ):
            assert required in block



def test_browser_worker_treats_pipeline_version_zero_as_a_valid_exact_binding():
    source = (ROOT / "services" / "browser-controller" / "browser_queue_worker.py").read_text(encoding="utf-8")
    block = source.split("def require_bound_approval(", 1)[1].split("\n\ndef require_current_input_hash", 1)[0]
    assert "bound_pipeline_version or -1" not in block
    assert "bound_pipeline_version is None" in block
    assert "int(app_row[3] or 0) != int(bound_pipeline_version)" in block


def test_delegated_integration_children_use_typed_parent_plan_and_version_bindings():
    source = (ROOT / "tests" / "integration" / "test_autofill_execution_lifecycle.py").read_text(encoding="utf-8")
    helper = source.split("def _insert_typed_delegated_child(", 1)[1].split("\n\nclass FakeTransport", 1)[0]
    for required in (
        "parent_approval_request_id", "bound_pipeline_version", "bound_autofill_plan_key",
        "bound_autofill_plan_id", "expected_target_id",
    ):
        assert required in helper
    denied = source.split("def test_denied_parent_restores_form_ready_and_closes_delegated_children(", 1)[1].split(
        "\ndef test_missing_delegated_child_is_repaired_and_bound_to_exact_parent", 1
    )[0]
    gate = source.split("def _configure_delegated_gate(", 1)[1].split(
        "\ndef test_parent_approved_child_pending_does_not_queue_browser_task", 1
    )[0]
    assert "_insert_typed_delegated_child(" in denied
    assert "_insert_typed_delegated_child(" in gate
    assert 'plan_key = task["autofill_plan_key"]' in denied
    assert 'plan_key = task["autofill_plan_key"]' in gate
