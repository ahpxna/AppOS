"""Strict project-bullet evidence checks for the fixed Word resume."""
from services.common.resume_project_bullet_audit import validate_bullet_change


def test_bullet_change_requires_jd_profile_evidence_and_change_log():
    baseline = {1: "Built an event vision pipeline for traffic data"}
    change = {
        "slot": 1,
        "text": "Built an event-camera data pipeline with calibrated labels for traffic analysis",
        "previous_bullet": baseline[1],
        "jd_requirement_quote": "event-camera data pipeline",
        "project_evidence_quote": "event-camera data pipeline with calibrated labels",
        "why_better": "Names the evidenced data-pipeline work that directly matches the quoted JD requirement.",
        "word_change_rationale": [
            {"before": "event vision", "after": "event-camera data pipeline with calibrated labels for traffic analysis",
             "why": "Uses the asset's specific pipeline evidence and the JD's exact requirement, not a generic vision phrase."}
        ],
    }
    assert validate_bullet_change(
        change, baseline_bullets=baseline, jd_text="Need experience with an event-camera data pipeline.",
        asset_source="The project built an event-camera data pipeline with calibrated labels.",
    ) == []


def test_bullet_change_blocks_unconfirmed_tool_claims():
    change = {
        "slot": 1, "text": "Built a Kubernetes event-camera platform", "previous_bullet": "Built an event vision pipeline",
        "jd_requirement_quote": "event-camera platform", "project_evidence_quote": "Kubernetes production deployment",
        "why_better": "Uses a more relevant platform term.",
        "word_change_rationale": [{"before": "vision pipeline", "after": "Kubernetes platform", "why": "Claims closer job relevance."}],
    }
    errors = validate_bullet_change(
        change, baseline_bullets={1: "Built an event vision pipeline"},
        jd_text="Need an event-camera platform.", asset_source="Built an academic event vision pipeline.",
    )
    assert any("absent from the cited profile asset" in error for error in errors)
