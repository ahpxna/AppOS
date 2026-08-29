"""Post-write verification state machine for deterministic autofill."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from services.autofill.autofill_planner_v1 import PlannedAction


@dataclass(frozen=True)
class VerificationResult:
    status: str  # completed | partial | needs_review
    verified_refs: tuple[str, ...]
    failed_refs: tuple[str, ...]


def verify_actions(
    actions: list[PlannedAction],
    observed_values: Mapping[str, str],
    *,
    value_matches: Callable[[PlannedAction, str], bool] | None = None,
) -> VerificationResult:
    """Classify post-write verification after the caller resolves fresh refs.

    Session-level code owns semantic rematching after ATS/React rerenders; this
    module owns the common completed/partial/needs-review state machine. The
    optional comparator preserves role-aware normalization without weakening
    the default exact-value verifier used by existing callers/tests.
    """
    intended = [item for item in actions if item.action in {"fill", "select", "check", "upload", "verify"}]
    def matches(item: PlannedAction) -> bool:
        if item.ref not in observed_values:
            return False
        observed = observed_values[item.ref]
        return value_matches(item, observed) if value_matches is not None else observed == item.value
    verified = tuple(item.ref for item in intended if matches(item))
    failed = tuple(item.ref for item in intended if item.ref not in verified)
    pauses = any(item.action == "pause" for item in actions)
    if failed:
        return VerificationResult("partial", verified, failed)
    if pauses:
        return VerificationResult("needs_review", verified, ())
    return VerificationResult("completed", verified, ())
