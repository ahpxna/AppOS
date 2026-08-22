from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from services.common.pointer_dynamics import Point, fit_regimes
from services.common.profile_job_matching import rank_job, unique_terms
from services.discovery.market_demand_intelligence_v1 import extract_signals


def test_job_rank_is_explainable_and_prioritises_title():
    result = rank_job(
        title="Python Security Engineer", jd_text="Security engineering: build APIs with Python and threat modeling.",
        profile_terms=["Python", "Threat Modeling"], user_keywords=["security"],
    )
    assert result["discovery_score"] == 11
    assert result["matched_profile_terms"] == ["python", "threat modeling"]
    assert result["matched_user_keywords"] == ["security"]
    assert unique_terms([" Python ", "python", "", "API Security"]) == ["python", "api security"]


def test_pointer_fit_keeps_local_regimes_not_one_global_average():
    points = [Point(float(t), 2.0 * t if t < 8 else 16.0 + 7.0 * (t - 8), float(t % 3)) for t in range(20)]
    regimes = fit_regimes(points, window=8, stride=4)
    assert len(regimes) == 4
    assert regimes[0]["drift"]["x"] == 2.0
    assert regimes[-1]["drift"]["x"] == 7.0
    assert len(regimes[0]["drift_samples"]) == 7
    assert regimes[0]["diffusion"]["estimator"].startswith("mad_")


def test_market_signals_keep_literal_jd_evidence():
    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return '''{
          "requirements": [
            {
              "name": "Temporal",
              "category": "platform",
              "importance": "required",
              "evidence_quote": "Experience building workflows with Temporal is required."
            },
            {
              "name": "invented technology",
              "category": "tool",
              "importance": "required",
              "evidence_quote": "This sentence is not present in the JD."
            }
          ]
        }'''

    signals = extract_signals(
        "This role is not a fit for the candidate. Experience building workflows with Temporal is required.",
        llm_call=fake_llm,
    )
    keys = {signal["normalized_keyword"] for signal in signals}
    assert keys == {"temporal"}  # No static catalogue: an unfamiliar name is retained.
    assert signals[0]["requirement_type"] == "platform"
    assert signals[0]["importance"] == "required"
    assert signals[0]["evidence_excerpt"] == "Experience building workflows with Temporal is required."
    assert calls[0]["role"] == "market_intelligence"
