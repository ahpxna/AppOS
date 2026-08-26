"""Regression tests for official-resume employment evidence feeding JD tailoring."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from tests.psycopg_stub_utils import install_if_missing, restore

os.environ.setdefault("JOBOS_DB_PASSWORD", "unit-test")
_psycopg_saved = install_if_missing()

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


evidence = _load(
    "jobos_profile_evidence_employment_test",
    "services/profile-ingestion/build_profile_evidence_units_qwen_v1.py",
)
synth = _load(
    "jobos_profile_asset_employment_test",
    "services/profile-ingestion/synthesize_profile_assets_qwen_v1.py",
)
restore(_psycopg_saved)


def _employment_unit(quote: str = "Investigated security alerts and documented findings.") -> dict:
    return {
        "evidence_unit_id": "e1",
        "evidence_type": "employment_experience",
        "direct_quote": quote,
        "evidence_title": "Security internship evidence",
        "evidence_summary": "Internship work investigating alerts and documenting findings.",
        "supports_claims": ["Investigated security alerts"],
        "does_not_support_claims": ["Owned production SOC infrastructure"],
        "role_families": ["cybersecurity_analyst"],
        "competency_tags": ["alert investigation"],
        "tool_tags": [],
        "project_tags": [],
    }


def _asset(asset_type: str = "source_document_asset") -> dict:
    return {
        "asset_title": "Official resume employment evidence",
        "asset_type": asset_type,
        "canonical_narrative": (
            "The official resume records a Security Analyst internship with explicit alert "
            "investigation and documentation responsibilities. This narrative preserves the "
            "source boundary and does not infer production ownership or additional scope."
        ),
        "job_oriented_summary": "Use only the cited resume employment evidence.",
        "resume_bullet_bank": "",
        "interview_story": "",
        "cover_letter_positioning": "",
        "do_not_overclaim_rules": [],
    }


def _doc_row(document_type: str = "official_resume") -> tuple:
    return (
        "doc-1", "raw-1", "resume.docx", "file:///resume.docx", "Official Resume",
        document_type, "primary_profile_evidence", "Candidate-authored resume", [],
    )


def test_employment_experience_is_first_class_and_requires_verbatim_quote():
    unit = evidence.normalize_unit({
        "section_index": 2,
        "evidence_type": "employment_experience",
        "evidence_title": "Security internship",
        "direct_quote": "Investigated security alerts and documented findings.",
        "evidence_summary": "Investigated alerts during an internship.",
    })
    assert unit["evidence_type"] == "employment_experience"

    sections = {2: {"section_text": "Investigated security alerts and documented findings."}}
    assert evidence.validate_unit_source_grounding(unit, sections) == (True, "ok")

    hallucinated = dict(unit, direct_quote="Owned the production SOC.")
    ok, reason = evidence.validate_unit_source_grounding(hallucinated, sections)
    assert not ok and reason == "direct_quote_not_verbatim_in_source_section"

    missing = dict(unit, direct_quote="")
    ok, reason = evidence.validate_unit_source_grounding(missing, sections)
    assert not ok and reason == "employment_experience_requires_verbatim_direct_quote"


def test_official_resume_compiles_to_source_document_asset_with_verbatim_employment_anchor():
    assert synth.fallback_asset_type("official_resume") == "source_document_asset"
    employment = [_employment_unit()]
    asset = synth.attach_official_resume_source_quotes(_asset(), "official_resume", employment)
    assert "VERBATIM EMPLOYMENT SOURCE QUOTES" in asset["resume_bullet_bank"]
    assert employment[0]["direct_quote"] in asset["resume_bullet_bank"]
    assert synth.validate_asset_basic(asset, _doc_row(), employment) == (True, "ok")


def test_employment_language_is_rejected_without_official_resume_employment_evidence():
    asset = _asset("strategic_asset")
    asset["canonical_narrative"] += " It describes professional experience in security operations."
    ok, reason = synth.validate_asset_basic(asset, _doc_row("guidance_profile"), [_employment_unit()])
    assert not ok and reason == "employment_language_requires_official_resume"

    official = _asset()
    official["canonical_narrative"] += " It describes professional experience in security operations."
    ok, reason = synth.validate_asset_basic(official, _doc_row(), [{"evidence_type": "technical_skill"}])
    assert not ok and reason == "employment_language_requires_employment_evidence"


def test_official_resume_prompts_treat_titles_as_immutable_but_bullets_as_tailorable():
    doc = _doc_row()
    # Evidence builder uses a different row shape (no storage_url).
    evidence_doc = (doc[0], doc[1], doc[2], doc[4], doc[5], doc[6], doc[7], doc[8])
    eprompt = evidence.build_prompt(evidence_doc, [{
        "section_index": 1,
        "section_title": "PROFESSIONAL EXPERIENCE",
        "section_type": "experience",
        "section_text": "Security Intern | Example Co | 2025\nInvestigated alerts.",
    }])
    assert "employment_experience" in eprompt
    assert "verbatim direct_quote" in eprompt

    sprompt = synth.build_prompt(doc, [_employment_unit()])
    assert "official resume" in sprompt.lower()
    assert "Do not alter employer, job title, dates" in sprompt
    assert "Bullet language may be job-oriented" in sprompt
    assert "tailored only within this evidence" not in synth.SYSTEM_PROMPT
    assert "may later reframe an existing bullet JD-first" in synth.SYSTEM_PROMPT


def test_profile_ready_versions_force_new_grounding_contract_to_be_rebuilt():
    canonical = (ROOT / "scripts/jobos_profile_ready.py").read_text(encoding="utf-8")
    shim = (ROOT / "jobos_profile_ready.py").read_text(encoding="utf-8")
    assert 'GENERIC_EVIDENCE_VERSION = "profile_evidence_unit_builder_qwen_v2_2026_08_25"' in canonical
    assert 'scripts/jobos_profile_ready.py' in shim
    assert 'GENERIC_ASSET_VERSION = "profile_asset_synthesizer_qwen_v2_2026_08_25"' in canonical
    assert 'runpy.run_path' in shim
