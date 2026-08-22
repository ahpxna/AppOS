"""Pure readiness-gate tests; no database, browser, or model is contacted."""

from services.orchestrator.pipeline_preflight_v1 import (
    BASE_PACK_PURPOSES,
    assess_profile_gate,
    missing_base_packs,
)


def test_missing_base_pack_error_is_explained_by_upstream_gates():
    result = assess_profile_gate(
        approved_assets=0,
        approved_capabilities=0,
        fresh_briefs=2,
        present_packs=set(),
    )

    assert result["status"] == "blocked"
    assert "no approved profile assets" in result["detail"]
    assert "base_fit_check_support" in result["missing_packs"]
    assert "prepare_profile_for_pipeline" in result["remediation"]


def test_complete_profile_gate_is_ready_only_when_every_base_pack_exists():
    present = set(BASE_PACK_PURPOSES)
    assert missing_base_packs(present) == []
    result = assess_profile_gate(
        approved_assets=2,
        approved_capabilities=3,
        fresh_briefs=5,
        present_packs=present,
    )
    assert result["status"] == "pass"
