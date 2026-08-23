"""Create allowed, narrow autofill actions from fields and approved values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.autofill.field_matcher_v1 import FieldClass, FieldMatch, match_field
from services.autofill.form_inspector_v1 import FormField, QuestionGroup
from services.common.immigration_semantics import classify_immigration_question


@dataclass(frozen=True)
class PlannedAction:
    action: str  # fill | select | check | upload | verify | pause
    ref: str
    value: str | None
    profile_key: str | None
    reason: str
    question_label: str | None = None


def _lookup(profile: Mapping[str, Any], key: str) -> Any:
    value: Any = profile
    for part in key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _answer_for_question(question: str, answers: Mapping[str, Any]) -> str | None:
    kind = classify_immigration_question(question)
    if kind is None:
        return None
    record = answers.get(kind.value)
    if not isinstance(record, Mapping) or not record.get("confirmed_at") or int(record.get("confirmation_version") or 0) < 1:
        return None
    value = str(record.get("value") or "").strip()
    return value if value.casefold() in {"yes", "no"} else None


def _option_for_value(group: QuestionGroup, value: Any):
    wanted = str(value).strip().casefold()
    return next((item for item in group.options if item.label.casefold() == wanted), None)


def plan_autofill(
    fields: list[FormField], profile: Mapping[str, Any], *,
    question_groups: list[QuestionGroup] | None = None,
    approved_sensitive_answers: Mapping[str, Any] | None = None,
) -> tuple[list[PlannedAction], list[FieldMatch]]:
    """Plan only exact mappings with a provably safe verification path."""
    actions: list[PlannedAction] = []
    matches: list[FieldMatch] = []
    used_option_refs: set[str] = set()
    answers = approved_sensitive_answers or {}
    for group in question_groups or []:
        pseudo = FormField(group.options[0].ref, group.label, group.role, required=group.required)
        match = match_field(pseudo)
        matches.append(match)
        value = _answer_for_question(group.label, answers) if match.field_class is FieldClass.SENSITIVE else (
            _lookup(profile, match.profile_key) if match.profile_key else None
        )
        option = _option_for_value(group, value) if value not in (None, "") else None
        if option is None:
            actions.append(PlannedAction("pause", pseudo.ref, None, match.profile_key, match.reason, group.label))
            continue
        used_option_refs.update(item.ref for item in group.options)
        actions.append(PlannedAction(
            "verify" if option.selected is True else "check", option.ref, str(value), match.profile_key,
            "Exact classified answer and option state are available.", group.label,
        ))
    for field in fields:
        if field.ref in used_option_refs or field.role in {"radiogroup", "group", "fieldset", "list"}:
            continue
        match = match_field(field)
        matches.append(match)
        if match.field_class is FieldClass.SENSITIVE:
            actions.append(PlannedAction("pause", field.ref, None, None, match.reason, field.label))
            continue
        if match.field_class is FieldClass.UNKNOWN or match.profile_key is None:
            actions.append(PlannedAction("pause", field.ref, None, None, match.reason, field.label))
            continue
        value = _lookup(profile, match.profile_key)
        if value in (None, ""):
            actions.append(PlannedAction("pause", field.ref, None, match.profile_key, "No approved profile value exists.", field.label))
        elif match.field_class is FieldClass.DERIVED:
            actions.append(PlannedAction("pause", field.ref, None, match.profile_key, "Derived answer requires evidence review.", field.label))
        elif match.profile_key.startswith("documents."):
            actions.append(PlannedAction("upload", field.ref, str(value), match.profile_key, match.reason, field.label))
        elif field.role in {"combobox", "listbox", "select"}:
            actions.append(PlannedAction("select", field.ref, str(value), match.profile_key, match.reason, field.label))
        elif field.role in {"checkbox", "radio"}:
            actions.append(PlannedAction("pause", field.ref, None, match.profile_key, "Control is missing an explicit question-group model.", field.label))
        else:
            actions.append(PlannedAction("fill", field.ref, str(value), match.profile_key, match.reason, field.label))
    return actions, matches
