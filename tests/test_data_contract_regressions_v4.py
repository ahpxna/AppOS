from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.psycopg_stub_utils import install_if_missing, restore

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    saved = install_if_missing()
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        restore(saved)


def test_coerce_int_rejects_container_boolean_and_non_integral_values():
    from services.common.value_coercion import coerce_int

    assert coerce_int(3) == 3
    assert coerce_int("3") == 3
    assert coerce_int(3.0) == 3
    assert coerce_int(True, default=-1) == -1
    assert coerce_int({"value": 3}, default=-1) == -1
    assert coerce_int([3], default=-1) == -1
    assert coerce_int("3.5", default=-1) == -1
    assert coerce_int("not-a-number", default=-1) == -1


def test_malformed_confirmed_immigration_answer_degrades_to_no_answer():
    planner = _load("audit_autofill_planner_v4", "services/autofill/autofill_planner_v1.py")
    answers = {
        "requires_future_sponsorship": {
            "confirmed_at": "2026-08-27T00:00:00Z",
            "confirmation_version": {"bad": 1},
            "value": "yes",
        }
    }
    assert planner._answer_for_question("Will you require sponsorship in the future?", answers) is None


def test_malformed_action_scope_version_fails_closed_instead_of_raising():
    from services.common.autofill_action_scope import action_is_exactly_approved

    class Action:
        action = "fill"
        ref = "email"
        question_label = "Email"
        profile_key = "personal.email"
        value = "candidate@example.com"

    assert action_is_exactly_approved(Action(), {"version": {"bad": 3}, "actions": []}) is False
    assert action_is_exactly_approved(Action(), {"version": "garbage", "actions": []}) is False


def test_document_generator_soft_degrades_malformed_nested_model_fields():
    generator = _load("audit_document_generator_v4", "services/document-generation/generate_documents_v1.py")

    content, used, evidence, dropped = generator.validate_and_render(
        "resume",
        {
            "experience_updates": [{"slot": 1, "text": {"bad": "shape"}}],
            "skill_lines": [{"category": "Skills", "items": "Python", "source_asset_id": {"id": "asset-a"}}],
            "project_subtitle_updates": {"slot": 1, "text": "not-a-list"},
        },
        {"asset-a"},
        experience_baselines={1: {"previous_bullet": "Baseline", "header_context": "Analyst | Acme", "max_chars": 220}},
        baseline_bullets={},
        baseline_subtitles={},
        jd_text="Python security analysis and incident response experience required.",
    )
    assert content == ""
    assert used == []
    assert any("unknown asset" in item for item in dropped)


def test_document_generator_malformed_short_answer_text_becomes_user_input():
    generator = _load("audit_document_generator_short_v4", "services/document-generation/generate_documents_v1.py")
    content, used, evidence, _dropped = generator.validate_and_render(
        "short_answers",
        {"answers": [{
            "question": "Why this role?",
            "answerable": True,
            "text": {"bad": "shape"},
            "source_asset_id": "asset-a",
        }]},
        {"asset-a"},
        expected_questions=["Why this role?"],
    )
    assert "[NEEDS USER INPUT]" in content
    assert used == []
    assert evidence["claims"][0]["answerable"] is False


def test_message_reply_malformed_source_asset_id_is_dropped_not_crash():
    messaging = _load("audit_message_reply_v4", "services/messaging/message_reply_v1.py")
    body, used, evidence = messaging.validate_reply({
        "sentences": [{
            "text": "I have five years of SOC leadership experience.",
            "kind": "claim",
            "source_asset_id": {"id": "asset-a"},
        }]
    }, {"asset-a"})
    assert body == ""
    assert used == []
    assert evidence["claims"] == []
    assert evidence["dropped_ungrounded_claims"]


def test_browser_task_url_container_is_controlled_permanent_error():
    worker = _load("audit_browser_worker_v4", "services/browser-controller/browser_queue_worker.py")
    with pytest.raises(worker.PermanentTaskError, match="url is required"):
        worker.require_url(None, {"url": {"href": "https://example.com"}})


def test_resume_requirement_catalog_does_not_character_split_scalar_shapes():
    from services.common.document_prompt_templates_v1 import material_requirement_summary, requirement_catalog

    assert requirement_catalog("Python") == []
    summary = material_requirement_summary({
        "matched_requirements": "Python",
        "missing_or_weak_requirements": "AWS",
    })
    assert summary["supportable"] == []
    assert summary["unsupported"] == []
    assert summary["total_material_requirements"] == 0


def test_fit_next_step_is_deterministic_not_model_authoritative():
    fit = _load("audit_job_fit_next_step_v4", "services/job-analysis/analyze_job_fit_v1.py")

    approved = fit.normalize_analysis({"fit_score": 90, "next_step": {"bad": "shape"}})
    review = fit.normalize_analysis({"fit_score": 70, "next_step": "submit_application"})
    rejected = fit.normalize_analysis({"fit_score": 30, "next_step": ["approve_research"]})

    assert approved["next_step"] == "approve_research"
    assert review["next_step"] == "ask_user_to_review_fit"
    assert rejected["next_step"] == "save_only_reject_by_fit"


@pytest.mark.parametrize("name,relative", [
    ("audit_profile_units_v4", "services/profile-ingestion/build_profile_evidence_units_qwen_v1.py"),
    ("audit_profile_assets_v4", "services/profile-ingestion/synthesize_profile_assets_qwen_v1.py"),
    ("audit_structured_units_v4", "services/profile-ingestion/build_structured_evidence_units_qwen_v2.py"),
    ("audit_structured_assets_v4", "services/profile-ingestion/synthesize_structured_tool_workflow_assets_qwen_v1.py"),
    ("audit_structured_audit_v4", "services/profile-ingestion/audit_structured_tool_workflow_assets_deepseek_v1.py"),
])
def test_profile_string_list_normalizers_skip_nested_objects(name, relative):
    module = _load(name, relative)
    assert module.clean_list(["Python", {"tool": "AWS"}, ["nested"], True, "  Linux  "]) == ["Python", "Linux"]


def test_profile_document_mapper_string_lists_skip_nested_objects():
    mapper = _load("audit_profile_mapper_list_v4", "services/profile-ingestion/map_profile_documents_qwen_v1.py")
    result = mapper.normalize_result({
        "risk_notes": ["Valid note", {"bad": "shape"}, ["nested"], "  Another note  "],
    })
    assert result["risk_notes"] == ["Valid note", "Another note"]


def test_interview_prep_nested_shape_drift_soft_degrades():
    prep = _load("audit_interview_prep_v4", "services/interview-prep/interview_prep_v1.py")
    normalized = prep.normalize_prep_result({
        "prep_notes": {"bad": "shape"},
        "opening_line": ["bad"],
        "questions_to_ask": ["Ask about priorities", {"bad": 1}],
        "stories_to_practice": "not-a-list",
        "watch_outs": [True, "Clarify scope"],
        "self_check": {"bad": 1},
    })
    assert normalized["prep_notes"].startswith("No grounded interview-prep notes")
    assert normalized["opening_line"] == ""
    assert normalized["questions_to_ask"] == ["Ask about priorities"]
    assert normalized["stories_to_practice"] == []
    assert normalized["watch_outs"] == ["Clarify scope"]
    assert normalized["self_check"] == ""
