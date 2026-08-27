from __future__ import annotations

import sys

import pytest

from services.application_actions.privileged_action_v1 import canonical_pipeline_step_for_browser_state
from services.autofill.value_normalization import equivalent_value, resolve_select_option
from services.runtime.openclaw_runtime import GlobalOpenClawForbiddenError, resolve_managed_openclaw
from services.runtime.process_runner import ProcessRunner


def test_process_runner_normalizes_timeout_without_raising():
    result = ProcessRunner().run(
        [sys.executable, "-c", "import time; time.sleep(2)"], timeout_s=0.1,
    )
    assert result.ok is False
    assert result.timed_out is True
    assert result.transient is True
    assert result.start_error


def test_browser_state_contract_collapses_manual_sso_only_at_pipeline_boundary():
    assert canonical_pipeline_step_for_browser_state("needs_manual_sso") == "needs_account_auth"
    assert canonical_pipeline_step_for_browser_state("authenticated") == "application_form_ready"
    assert canonical_pipeline_step_for_browser_state("unknown") is None


def test_autofill_normalization_requires_unique_option_and_preserves_job_values():
    assert equivalent_value(actual="FL", expected="Florida", role="select")
    assert equivalent_value(actual="+1 (212) 555-0100", expected="2125550100", label="phone")
    assert equivalent_value(actual="08/26/2026", expected="2026-08-26", input_type="date")
    assert resolve_select_option(["Florida", "Texas"], "FL").status == "unique_alias"
    assert resolve_select_option(["Florida", "FL"], "Florida").status == "exact"
    assert resolve_select_option(["Florida", "Florida"], "Florida").status == "ambiguous"


def test_openclaw_rejects_global_environment_override(monkeypatch):
    monkeypatch.setenv("OPENCLAW_BIN", "/usr/local/bin/openclaw")
    with pytest.raises(GlobalOpenClawForbiddenError):
        resolve_managed_openclaw(required=False)
