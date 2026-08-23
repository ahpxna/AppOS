"""Post-write verification state machine for deterministic autofill."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from services.autofill.autofill_planner_v1 import PlannedAction


@dataclass(frozen=True)
class VerificationResult:
    status: str  # completed | partial | needs_review
    verified_refs: tuple[str, ...]
    failed_refs: tuple[str, ...]


def verify_actions(actions: list[PlannedAction], observed_values: Mapping[str, str]) -> VerificationResult:
    intended = [item for item in actions if item.action in {"fill", "select", "check", "upload"}]
    verified = tuple(item.ref for item in intended if observed_values.get(item.ref) == item.value)
    failed = tuple(item.ref for item in intended if item.ref not in verified)
    pauses = any(item.action == "pause" for item in actions)
    if failed:
        return VerificationResult("partial", verified, failed)
    if pauses:
        return VerificationResult("needs_review", verified, ())
    return VerificationResult("completed", verified, ())
