"""Regression tests for separate candidate and company evidence in L6."""

import importlib.util
from pathlib import Path


_path = Path(__file__).parent / "services/document-generation/generate_documents_v1.py"
_spec = importlib.util.spec_from_file_location("jobos_document_generator", _path)
docgen = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(docgen)

_verifier_path = Path(__file__).parent / "services/document-generation/verify_document_truth_v1.py"
_verifier_spec = importlib.util.spec_from_file_location("jobos_document_verifier", _verifier_path)
verifier = importlib.util.module_from_spec(_verifier_spec)
assert _verifier_spec.loader is not None
_verifier_spec.loader.exec_module(verifier)


def test_cover_letter_keeps_only_known_company_sources():
    parsed = {
        "paragraphs": [
            {
                "text": "I am drawn to the company's published incident-response work.",
                "source_asset_id": "asset-1",
                "purpose": "motivation",
                "uses_company_context": True,
                "company_source_urls": ["https://company.example/about"],
                "company_insight": "The company publishes incident-response work.",
                "company_evidence_quote": "published incident-response work",
                "why_company_fit": "The published focus gives a concrete reason to discuss the candidate's security coursework.",
                "jd_requirement_quote": "Python analysis",
                "candidate_evidence_quote": "completed a course project using Python",
            },
            {
                "text": "I completed a course project using Python.",
                "source_asset_id": "asset-1",
                "purpose": "evidence",
                "uses_company_context": False,
                "company_source_urls": [],
                "jd_requirement_quote": "Python analysis",
                "candidate_evidence_quote": "completed a course project using Python",
            },
            {
                "text": "I admire an unsupported company claim.",
                "source_asset_id": "none",
                "purpose": "motivation",
                "uses_company_context": True,
                "company_source_urls": ["https://untrusted.example/post"],
            },
        ],
        "not_supported": [],
        "self_check": "grounded",
    }

    content, used, evidence, dropped = docgen.validate_and_render(
        "cover_letter", parsed, {"asset-1"}, {"https://company.example/about"}, jd_text="Python analysis is required."
    )

    assert "published incident-response" in content
    assert "course project" in content
    assert "unsupported company claim" not in content
    assert used == ["asset-1"]
    assert evidence["claims"][0]["company_source_urls"] == ["https://company.example/about"]
    assert any("unknown company URL" in item for item in dropped)


def test_cover_letter_prompt_exposes_only_sourced_company_context():
    app = {
        "company": "Example Co",
        "job_title": "Analyst",
        "fit_score": 80,
        "fit_decision": "approve_research",
        "risk_flags": [],
        "company_context": {
            "summary": "A source-backed summary.",
            "sources": ["https://company.example/about"],
        },
    }
    prompt = docgen.build_cover_letter_prompt(app, "[ASSET asset-1]")

    assert "SOURCED COMPANY CONTEXT" in prompt
    assert "https://company.example/about" in prompt
    assert "company_source_urls" in prompt
    assert "candidate_evidence_quote" in prompt
    assert "company-specific" in prompt


def test_truth_checker_fails_closed_for_unknown_company_source():
    results = verifier.verify_claims(
        cur=None,
        claims=[
            {
                "claim": "I admire the company mission.",
                "source_asset_id": "none",
                "uses_company_context": True,
                "company_source_urls": ["https://untrusted.example/post"],
            }
        ],
        model="not-called",
        ollama_url="http://not-called",
        timeout=1,
        temperature=0,
        num_ctx=1,
        verbose=False,
        valid_company_source_urls=set(),
    )

    assert results[0]["verdict"] == "unsupported"
    assert "no known company research source" in results[0]["reason"]


def test_resume_rejects_an_asset_bound_to_a_different_fixed_project_block():
    parsed = {
        "project_updates": [
            {
                "slot": 1,
                "text": "Implemented the project-specific pipeline.",
                "source_asset_id": "pki-asset",
                "supports_requirement": "Python",
            }
        ],
        "skill_lines_ranked": [],
    }
    content, used, evidence, dropped = docgen.validate_and_render(
        "resume", parsed, {"caroect-asset", "pki-asset"},
        fixed_project_assets={1: {"caroect-asset"}, 5: {"pki-asset"}},
    )

    assert not content
    assert not used
    assert evidence["resume_template"]["project_bullets"] == []
    assert any("not approved for fixed project slot 1-2" in item for item in dropped)
