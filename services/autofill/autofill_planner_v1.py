"""Create allowed, narrow autofill actions from fields and approved values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.autofill.field_matcher_v1 import FieldClass, FieldMatch, match_field
from services.autofill.form_inspector_v1 import FormField


@dataclass(frozen=True)
class PlannedAction:
    action: str  # fill | select | check | upload | pause
    ref: str
    value: str | None
    profile_key: str | None
    reason: str


def _lookup(profile: Mapping[str, Any], key: str) -> Any:
    value: Any = profile
    for part in key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def plan_autofill(
    fields: list[FormField], profile: Mapping[str, Any], *,
    approved_sensitive_answers: Mapping[str, str] | None = None,
) -> tuple[list[PlannedAction], list[FieldMatch]]:
    """Plan only values that are exact, approved, and safe to verify.

    Sensitive fields may be planned only if an adapter first maps exact question
    semantics to an explicit, user-confirmed key. This baseline deliberately
    leaves them paused.
    """
    actions: list[PlannedAction] = []
    matches: list[FieldMatch] = []
    for field in fields:
        match = match_field(field)
        matches.append(match)
        if match.field_class is FieldClass.SENSITIVE:
            actions.append(PlannedAction("pause", field.ref, None, None, match.reason))
            continue
        if match.field_class is FieldClass.UNKNOWN or match.profile_key is None:
            actions.append(PlannedAction("pause", field.ref, None, None, match.reason))
            continue
        value = _lookup(profile, match.profile_key)
        if value in (None, ""):
            actions.append(PlannedAction("pause", field.ref, None, match.profile_key, "No approved profile value exists."))
            continue
        if match.field_class is FieldClass.DERIVED:
            actions.append(PlannedAction("pause", field.ref, None, match.profile_key, "Derived answer requires evidence review."))
            continue
        if match.profile_key.startswith("documents."):
            actions.append(PlannedAction("upload", field.ref, str(value), match.profile_key, match.reason))
        elif field.role in {"combobox", "listbox", "select"}:
            actions.append(PlannedAction("select", field.ref, str(value), match.profile_key, match.reason))
        elif field.role in {"checkbox", "radio"}:
            actions.append(PlannedAction("pause", field.ref, None, match.profile_key, "Control state/options must be explicitly modelled before checking."))
        else:
            actions.append(PlannedAction("fill", field.ref, str(value), match.profile_key, match.reason))
    return actions, matches
