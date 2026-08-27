from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from services.common.company_research_sources import company_research_source_urls

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_company_research_sources_normalize_all_supported_storage_shapes():
    assert company_research_source_urls(["https://flat.example/a"]) == {"https://flat.example/a"}
    assert company_research_source_urls([
        {"type": "mock", "url": "https://legacy.example/a"},
    ]) == {"https://legacy.example/a"}
    assert company_research_source_urls({
        "urls": ["https://current.example/a", "https://current.example/b"],
        "not_found": ["mission"],
        "dropped_unsourced": ["https://not-authority.example/in-a-warning"],
    }) == {"https://current.example/a", "https://current.example/b"}


def test_company_context_and_truth_checker_read_current_and_legacy_source_shapes():
    generator = _load("audit_generate_documents", "services/document-generation/generate_documents_v1.py")
    verifier = _load("audit_verify_documents", "services/document-generation/verify_document_truth_v1.py")

    class One:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return (
                "acme.example", "summary", "mission", "products", [],
                {"urls": ["https://news.example/acme"]},
            )

    context = generator.fetch_company_context(One(), "Acme Security")
    assert context["sources"] == ["https://news.example/acme"]
    # URL-only legacy/current envelopes remain readable for provenance, but
    # unsourced generated prose is intentionally not promoted into a new
    # employer-facing claim.
    assert context["summary"] == ""

    class EvidenceAware(One):
        def fetchone(self):
            return (
                "acme.example", "summary", "mission", "products", [],
                {
                    "urls": ["https://news.example/acme"],
                    "field_evidence": {
                        "summary": [{
                            "source_url": "https://news.example/acme",
                            "supporting_quote": "Acme builds security tooling.",
                        }],
                    },
                },
            )

    grounded = generator.fetch_company_context(EvidenceAware(), "Acme Security")
    assert grounded["summary"] == "summary"

    class Many:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return [
                ({"urls": ["https://current.example/a"]},),
                ([{"type": "mock", "url": "https://legacy.example/a"}],),
                (["https://flat.example/a"],),
            ]

    assert verifier.fetch_company_source_urls(Many(), "app-1") == {
        "https://current.example/a", "https://legacy.example/a", "https://flat.example/a",
    }


def test_company_research_prompt_requests_the_quote_its_validator_requires():
    research = _load("audit_company_research", "services/research/company_research_v1.py")
    prompt = research.build_research_prompt("Acme Security", "acme.example")
    assert "supporting_quote" in prompt
    parsed = {
        "company_domain": "acme.example",
        "summary": "Grounded summary.",
        "mission": "",
        "products": "",
        "recent_news": [],
        "risks": [{
            "risk": "Layoffs", "detail": "A reduction was reported.",
            "source_url": "https://news.example/acme",
            "supporting_quote": "reduced its workforce",
        }],
        "sources": ["https://news.example/acme"],
        "not_found": [],
    }
    assert len(research.validate(parsed)["risks"]) == 1


def test_model_cannot_smuggle_factual_claim_through_courtesy_kind():
    messaging = _load("audit_message_reply", "services/messaging/message_reply_v1.py")
    safe, _ = messaging.no_source_reply_sentence_is_safe("Thank you for the update.", "courtesy")
    assert safe
    unsafe, reason = messaging.no_source_reply_sentence_is_safe(
        "Thank you for the update; I led a production SOC for five years.", "courtesy"
    )
    assert not unsafe and "factual claim" in reason

    unsafe_schedule, _ = messaging.no_source_reply_sentence_is_safe(
        "Tuesday at 2 PM works for me.", "scheduling"
    )
    assert not unsafe_schedule
    safe_schedule, _ = messaging.no_source_reply_sentence_is_safe(
        "I am happy to coordinate a time that works for the team.", "scheduling"
    )
    assert safe_schedule


def test_reply_truth_checker_does_not_trust_model_kind_label():
    messaging = _load("audit_message_reply_verify", "services/messaging/message_reply_v1.py")
    reply = {
        "id": "reply-1", "thread_id": "thread-1",
        "evidence_map": {"claims": [{
            "claim": "I led a production SOC for five years.",
            "source_asset_id": None, "kind": "courtesy",
        }]},
    }
    result = messaging.verify_reply_claims(
        None, reply, model="unused", ollama_url="http://127.0.0.1:1", timeout=1, ctx=256,
    )
    assert result["qa_status"] == "fail"
    assert result["qa_report"]["verdicts"][0]["verdict"] == "unsupported"


def test_fit_normalization_tolerates_numeric_strings_and_empty_blocker_placeholders():
    fit = _load("audit_fit_normalization", "services/job-analysis/analyze_job_fit_v1.py")
    result = fit.normalize_analysis({
        "fit_score": "82.5",
        "fit_decision": "reject",
        "hard_blockers": [{}, {"blocker": "", "reason": "placeholder"}, "none"],
        "role_family": "totally_off_schema",
    })
    assert result["fit_score"] >= 82
    assert result["fit_decision"] == "approve_research"
    assert result["hard_blockers"] == []
    assert result["role_family"] == "other"


def test_short_answer_questions_are_exact_bound_and_missing_answers_soft_degrade():
    generator = _load("audit_short_answers", "services/document-generation/generate_documents_v1.py")
    requested = "Why are you interested in this role?"
    parsed = {
        "answers": [
            {
                "question": "Invented question not requested",
                "answerable": True,
                "text": "Invented answer",
                "source_asset_id": "asset-1",
            }
        ]
    }
    content, used, evidence, dropped = generator.validate_and_render(
        "short_answers", parsed, {"asset-1"}, expected_questions=[requested]
    )
    assert requested in content
    assert "[NEEDS USER INPUT]" in content
    assert "Invented question not requested" not in content
    assert used == []
    assert evidence["claims"][0]["answerable"] is False
    assert any("not an exact requested question" in item for item in dropped)


def test_profile_mapper_rebinds_section_identity_to_authoritative_source_rows():
    mapper = _load("audit_profile_mapper", "services/profile-ingestion/map_profile_documents_qwen_v1.py")
    sections = [{
        "section_index": 1,
        "section_title": "PROFESSIONAL EXPERIENCE",
        "section_type": "experience",
        "section_text": "Security Intern | Example Co | 2025",
    }]
    normalized = mapper.normalize_result({
        "source_risk_level": "banana",
        "section_map": [
            {
                "section_index": 1,
                "section_title": "MODEL-SPOOFED TITLE",
                "semantic_type": "scope",
                "importance": "high",
                "summary": "Mapped experience",
                "supports_profile_assets": True,
            },
            {"section_index": 999, "section_title": "INVENTED SECTION", "semantic_type": "scope"},
        ],
    }, sections)
    assert normalized["source_risk_level"] == "medium"
    assert len(normalized["section_map"]) == 1
    assert normalized["section_map"][0]["section_title"] == "PROFESSIONAL EXPERIENCE"
    assert normalized["section_map"][0]["section_index"] == 1


def test_structured_evidence_provenance_and_tags_are_source_bound():
    structured = _load(
        "audit_structured_evidence",
        "services/profile-ingestion/build_structured_evidence_units_qwen_v2.py",
    )
    row = {
        "file_name": "official-notes.md",
        "section_title": "4.1 Network Analysis",
        "section_text": "Used Wireshark in Lab 01 for packet inspection. The report draws conclusions carefully.",
        "source_boundary_json": {"producer": "deterministic", "document": "wrong-model-cannot-change"},
    }
    raw = {
        "tool_name": "AWS",  # substring of "draws" must not count as source evidence
        "tool_tags": ["AWS", "Wireshark"],
        "project_tags": ["Invented Project"],
        "source_boundaries": {
            "document": "FAKE.pdf",
            "section": "FAKE SECTION",
            "courses": ["FAKE 999"],
            "labs": ["Fake Lab"],
            "projects": ["Invented Project"],
        },
        "claim": "Packet inspection workflow exposure.",
        "evidence_summary": "Packet inspection workflow exposure.",
    }
    unit = structured.normalize_unit(raw, row)
    assert unit["source_boundaries"]["document"] == "official-notes.md"
    assert unit["source_boundaries"]["section"] == "4.1 Network Analysis"
    assert "AWS" not in unit["tool_tags"]
    assert "Wireshark" in unit["tool_tags"]
    assert "Invented Project" not in unit["project_tags"]
    assert unit["source_boundaries"]["projects"] == []


def test_profile_evidence_tool_tag_grounding_does_not_accept_substrings_inside_words():
    evidence = _load(
        "audit_profile_evidence_tags",
        "services/profile-ingestion/build_profile_evidence_units_qwen_v1.py",
    )
    unit = {"tool_tags": ["AWS", "Wireshark"]}
    evidence._ground_tool_tags(unit, {
        "section_title": "Network lab",
        "section_text": "Wireshark captures were reviewed; the report draws conclusions carefully.",
    })
    assert unit["tool_tags"] == ["Wireshark"]


def test_asset_evidence_links_ignore_model_rank_and_deduplicate_indices():
    synth = _load(
        "audit_profile_asset_links",
        "services/profile-ingestion/synthesize_profile_assets_qwen_v1.py",
    )
    units = [
        {"id": "u1", "tool_tags": ["Wireshark"]},
        {"id": "u2", "tool_tags": ["Python"]},
    ]
    asset = {
        "evidence_links": [
            {"evidence_unit_index": 2, "evidence_rank": 99},
            {"evidence_unit_index": 2, "evidence_rank": 1},
            {"evidence_unit_index": 1, "evidence_rank": 1},
            {"evidence_unit_index": 999, "evidence_rank": 0},
        ],
        "tool_tags": ["Python", "InventedTool", "Wireshark"],
    }
    chosen = synth.selected_evidence_units(asset, units)
    assert [u["id"] for u in chosen] == ["u2", "u1"]
    synth.ground_asset_tool_tags(asset, units)
    assert asset["tool_tags"] == ["Python", "Wireshark"]


def test_json_object_parsers_reject_top_level_arrays_instead_of_leaking_shape_errors():
    modules = [
        ("audit_json_fit", "services/job-analysis/analyze_job_fit_v1.py", "extract_json_object"),
        ("audit_json_research", "services/research/company_research_v1.py", "extract_json_object"),
        ("audit_json_reply", "services/messaging/message_reply_v1.py", "extract_json_object"),
        ("audit_json_docs", "services/document-generation/generate_documents_v1.py", "extract_json_object"),
        ("audit_json_verify", "services/document-generation/verify_document_truth_v1.py", "extract_json_object"),
        ("audit_json_interview", "services/interview-prep/interview_prep_v1.py", "extract_json_object"),
        ("audit_json_mapper", "services/profile-ingestion/map_profile_documents_qwen_v1.py", "parse_json_content"),
        ("audit_json_evidence", "services/profile-ingestion/build_profile_evidence_units_qwen_v1.py", "parse_json_content"),
        ("audit_json_asset", "services/profile-ingestion/synthesize_profile_assets_qwen_v1.py", "parse_json_content"),
        ("audit_json_structured_evidence", "services/profile-ingestion/build_structured_evidence_units_qwen_v2.py", "parse_json_content"),
        ("audit_json_structured_asset", "services/profile-ingestion/synthesize_structured_tool_workflow_assets_qwen_v1.py", "parse_json_content"),
        ("audit_json_structured_audit", "services/profile-ingestion/audit_structured_tool_workflow_assets_deepseek_v1.py", "extract_json_object"),
    ]
    for name, path, fn_name in modules:
        module = _load(name, path)
        fn = getattr(module, fn_name)
        try:
            fn("```json\n[]\n```")
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError(f"{path}:{fn_name} accepted a top-level array")


def test_company_research_generated_fields_require_quote_bindings_but_degrade_instead_of_fail():
    research = _load("audit_company_research_evidence", "services/research/company_research_v1.py")
    source = "https://news.example/acme"
    no_binding = research.validate({
        "company_domain": "acme.example",
        "summary": "Generated summary without evidence",
        "mission": "Generated mission without evidence",
        "products": "Generated products without evidence",
        "sources": [source],
        "recent_news": [],
        "risks": [],
    })
    assert no_binding["summary"] == ""
    assert no_binding["mission"] == ""
    assert no_binding["products"] == ""
    assert len(no_binding["dropped_unsourced"]) >= 3

    grounded = research.validate({
        "company_domain": "acme.example",
        "summary": "Grounded summary",
        "mission": "",
        "products": "",
        "field_evidence": {
            "summary": [{"source_url": source, "supporting_quote": "Acme builds security tooling."}],
        },
        "sources": [source],
        "recent_news": [],
        "risks": [],
    })
    assert grounded["summary"] == "Grounded summary"
    assert grounded["field_evidence"]["summary"][0]["source_url"] == source


def test_external_boolean_coercion_does_not_treat_string_false_as_true():
    from services.common.value_coercion import coerce_bool

    assert coerce_bool(False) is False
    assert coerce_bool("false") is False
    assert coerce_bool("0") is False
    assert coerce_bool("no") is False
    assert coerce_bool(True) is True
    assert coerce_bool("true") is True
    assert coerce_bool("1") is True
    assert coerce_bool("yes") is True
    assert coerce_bool("definitely") is False


def test_company_research_application_domain_ignores_shared_ats_and_binds_real_hint():
    research = _load("audit_company_domain_binding", "services/research/company_research_v1.py")

    class Cur:
        def __init__(self, url):
            self.url = url

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return ("Acme Security", self.url)

    assert research.company_for_application(
        Cur("https://boards.greenhouse.io/acme/jobs/123"), "app-1"
    ) == ("Acme Security", None)
    assert research.company_for_application(
        Cur("https://jobs.lever.co/acme/abc"), "app-1"
    ) == ("Acme Security", None)
    assert research.company_for_application(
        Cur("https://careers.acme.example/jobs/123"), "app-1"
    ) == ("Acme Security", "careers.acme.example")

    parsed = {
        "company_domain": "model-spoofed.example",
        "summary": "",
        "mission": "",
        "products": "",
        "sources": ["https://acme.example/blog/layoffs"],
        "recent_news": [],
        "risks": [{
            "risk": "Layoffs",
            "detail": "Reported on the company blog.",
            "source_url": "https://acme.example/blog/layoffs",
            "supporting_quote": "workforce reduction",
        }],
    }
    validated = research.validate(parsed, expected_domain="https://www.acme.example/about")
    assert validated["company_domain"] == "acme.example"
    assert validated["risks"] == []
    assert any("self-sourced" in item for item in validated["dropped_unsourced"])


def test_reply_subject_is_derived_from_thread_and_model_cannot_invent_it():
    messaging = _load("audit_reply_subject_binding", "services/messaging/message_reply_v1.py")
    history = [{
        "id": "m-1", "direction": "inbound", "sender": "recruiter@example.com",
        "subject": "Interview next steps\r\nInjected-Header: nope", "body": "Hello", "at": "now",
    }]
    subject, source_id = messaging.derive_reply_subject(history)
    assert subject == "Re: Interview next steps Injected-Header: nope"
    assert source_id == "m-1"
    prompt = messaging.build_reply_prompt(
        {"company": "Acme", "person_name": "R", "person_role": "Recruiter",
         "job_title": "Analyst"}, history, [], "general_reply",
    )
    assert '"subject"' not in prompt
    assert "derived deterministically" in prompt


def test_ats_adapters_soft_degrade_shape_drift_and_string_false_remote(monkeypatch):
    ats = _load("audit_ats_shape_drift", "services/discovery/ats_discovery_v1.py")

    monkeypatch.setattr(ats, "http_get_json", lambda _url: [])
    assert ats.fetch_greenhouse("acme") == []

    monkeypatch.setattr(ats, "http_get_json", lambda _url: {"unexpected": "shape"})
    assert ats.fetch_lever("acme") == []
    assert ats.fetch_breezy("acme") == []

    monkeypatch.setattr(ats, "http_get_json", lambda _url: {
        "jobs": [{
            "id": "1", "title": "Security Analyst", "location": "New York, NY",
            "department": "Security", "isRemote": "false",
            "jobUrl": "https://jobs.ashbyhq.com/acme/1",
            "descriptionPlain": "Investigate alerts and document findings. " * 10,
        }]
    })
    jobs = ats.fetch_ashby("acme")
    assert len(jobs) == 1
    assert jobs[0]["remote"] is False


def test_linkedin_completed_task_accepts_nested_result_envelopes(monkeypatch):
    linkedin = _load("audit_linkedin_nested_result", "services/discovery/linkedin_intake_v1.py")
    jd = "Security analyst responsibilities and qualifications. " * 10

    class Cur:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return (
                "completed",
                {"url": "https://www.linkedin.com/jobs/view/123/"},
                {"parsed": {"text": jd}},
                None,
            )

    captured = {}

    def fake_intake(_cur, **kwargs):
        captured.update(kwargs)
        return "app-1"

    monkeypatch.setattr(linkedin, "intake", fake_intake)
    args = type("Args", (), {
        "task_id": "task-1", "company": "Acme", "title": "Analyst",
        "location": "", "work_mode": "",
    })()
    assert linkedin.cmd_ingest_task(Cur(), args) == 0
    assert captured["jd_text"] == jd.strip()
    assert captured["url"] == "https://www.linkedin.com/jobs/view/123/"


def test_linkedin_import_skips_bad_records_without_discarding_valid_records(monkeypatch, tmp_path, capsys):
    linkedin = _load("audit_linkedin_mixed_import", "services/discovery/linkedin_intake_v1.py")
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps([
        "not-an-object",
        {"company": "Acme", "title": "Analyst", "url": "https://www.linkedin.com/jobs/view/123/",
         "jd_text": "Security analyst responsibilities and qualifications. " * 10},
    ]), encoding="utf-8")

    monkeypatch.setattr(linkedin, "intake", lambda _cur, **_kwargs: "app-1")
    args = type("Args", (), {"file": path, "apply": False})()
    assert linkedin.cmd_import(object(), args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["created"] == 1
    assert output["invalid"] == 1
    assert output["duplicates"] == 0


def test_short_answer_string_false_is_not_promoted_to_answerable():
    generator = _load("audit_short_answer_bool", "services/document-generation/generate_documents_v1.py")
    question = "Are you willing to relocate?"
    parsed = {"answers": [{
        "question": question,
        "answerable": "false",
        "text": "Yes, I am willing to relocate.",
        "source_asset_id": "asset-1",
        "missing_information": "User relocation preference is not known.",
    }]}
    content, used, evidence, _dropped = generator.validate_and_render(
        "short_answers", parsed, {"asset-1"}, expected_questions=[question]
    )
    assert "[NEEDS USER INPUT]" in content
    assert "Yes, I am willing to relocate." not in content
    assert used == []
    assert evidence["claims"][0]["answerable"] is False


def test_ats_adapters_soft_degrade_nested_shape_drift(monkeypatch):
    ats = _load("audit_ats_nested_shape_drift", "services/discovery/ats_discovery_v1.py")

    payloads = {
        "greenhouse": {"jobs": [{
            "id": 1, "title": "Analyst", "location": "not-an-object",
            "departments": ["bad"], "content": {"bad": "html"},
            "absolute_url": ["bad"],
        }]},
        "lever": [{
            "id": 2, "text": "Analyst", "categories": "bad",
            "lists": ["bad"], "description": {"bad": "html"},
            "hostedUrl": {"bad": "url"},
        }],
        "smart": {"content": [{
            "id": "3", "name": "Analyst", "location": "bad",
            "department": "bad", "function": ["bad"],
        }]},
        "workable": {"jobs": [{
            "shortcode": "4", "title": "Analyst", "location": "bad",
            "department": {"bad": "dept"},
        }]},
    }

    monkeypatch.setattr(ats, "http_get_json", lambda _url: payloads["greenhouse"])
    assert ats.fetch_greenhouse("acme")[0]["location"] == ""

    monkeypatch.setattr(ats, "http_get_json", lambda _url: payloads["lever"])
    assert ats.fetch_lever("acme")[0]["department"] == ""

    monkeypatch.setattr(ats, "http_get_json", lambda _url: payloads["smart"])
    assert ats.fetch_smartrecruiters("acme")[0]["location"] == ""

    monkeypatch.setattr(ats, "http_get_json", lambda _url: payloads["workable"])
    assert ats.fetch_workable("acme")[0]["department"] == ""
