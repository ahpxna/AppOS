"""Strict checks for editable text between project name and GitHub link."""
from services.common.resume_project_header_audit import validate_subtitle_change


def test_subtitle_change_requires_exact_jd_evidence_and_word_reasons():
    baseline = {1: "Sim-to-Real Event Vision"}
    change = {
        "slot": 1,
        "text": "Event-Camera Dataset Engineering",
        "previous_subtitle": "Sim-to-Real Event Vision",
        "jd_requirement_quote": "event-camera data pipeline",
        "project_evidence_quote": "event-camera data pipeline",
        "why_better": "Names the evidenced dataset work that directly addresses the quoted role requirement.",
        "word_change_rationale": [
            {"before": "Sim-to-Real Event Vision", "after": "Event-Camera Dataset Engineering",
             "why": "Replaces broad research wording with the specific evidenced pipeline work requested by the JD."}
        ],
    }
    assert validate_subtitle_change(
        change, baseline_subtitles=baseline, jd_text="Candidates need an event-camera data pipeline.",
        asset_source="Built an event-camera data pipeline for simulation and real capture.",
    ) == []


def test_subtitle_change_fails_closed_when_the_project_quote_is_not_in_asset():
    change = {
        "slot": 1, "text": "Event-Camera Dataset Engineering", "previous_subtitle": "Sim-to-Real Event Vision",
        "jd_requirement_quote": "event-camera data pipeline", "project_evidence_quote": "invented production deployment",
        "why_better": "Claims a closer JD fit.",
        "word_change_rationale": [{"before": "Sim-to-Real Event Vision", "after": "Event-Camera Dataset Engineering", "why": "Specific required change."}],
    }
    errors = validate_subtitle_change(
        change, baseline_subtitles={1: "Sim-to-Real Event Vision"},
        jd_text="Candidates need an event-camera data pipeline.", asset_source="Academic event-camera data pipeline.",
    )
    assert any("absent from the cited project asset" in error for error in errors)
