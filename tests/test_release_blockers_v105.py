from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_linkedin_discovery_uses_canonical_observation_boundary():
    source = (ROOT / "services/discovery/linkedin_discovery_v1.py").read_text()
    assert source.count("find_and_observe_existing(") >= 2
    assert "WHERE source = 'linkedin' AND source_job_id" not in source


def test_render_completion_can_share_artifact_transaction():
    registry = (ROOT / "services/common/artifact_registry_v1.py").read_text()
    renderer = (ROOT / "services/review/render_review_artifacts_v1.py").read_text()
    assert "pdf_artifact_id: str | None = None, cur=None" in registry
    assert "pdf_artifact_id=pdf_artifact_id, cur=cur" in renderer
    assert "SAVEPOINT jobos_review_render" in renderer


def test_company_research_consumers_use_exact_identity_key():
    for relative in (
        "services/research/company_research_v1.py",
        "services/document-generation/generate_documents_v1.py",
        "services/document-generation/verify_document_truth_v1.py",
        "services/interview-prep/interview_prep_v1.py",
    ):
        assert "identity_key" in (ROOT / relative).read_text(), relative


def test_orchestrator_retries_are_durably_scheduled():
    source = (ROOT / "services/orchestrator/orchestrator_v1.py").read_text()
    assert "orchestrator_next_attempt_at" in source
    assert "durable backoff scheduled" in source


def test_revision_worker_recovers_verified_resume_state():
    source = (ROOT / "services/review/document_revision_worker_v1.py").read_text()
    assert 'expected_from="docs_failed_qa"' in source
    assert 'to="docs_verified"' in source
