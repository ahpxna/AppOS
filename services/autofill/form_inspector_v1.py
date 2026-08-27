"""Normalize ATS accessibility snapshots into transport-neutral form models."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class FormField:
    ref: str
    label: str
    role: str
    value: str = ""
    required: bool = False
    selected: bool | None = None
    options: tuple[str, ...] = ()
    document_hint: str | None = None
    input_type: str = ""


@dataclass(frozen=True)
class QuestionOption:
    ref: str
    label: str
    selected: bool | None


@dataclass(frozen=True)
class QuestionGroup:
    label: str
    role: str
    options: tuple[QuestionOption, ...]
    required: bool = False


def document_hint(label: str, role: str) -> str | None:
    text = label.casefold()
    if role not in {"file", "fileinput", "button", "textbox"} and "upload" not in text:
        return None
    if re.search(r"\b(resume|résumé|cv)\b", text):
        return "resume"
    if "cover" in text and "letter" in text:
        return "cover_letter"
    return None


def inspect_nodes(nodes: Iterable[dict[str, Any]]) -> list[FormField]:
    """Build fields from the existing OpenClaw snapshot parser's node format."""
    fields: list[FormField] = []
    for node in nodes:
        ref, label = str(node.get("ref") or ""), str(node.get("label") or "").strip()
        if not ref or not label:
            continue
        role = str(node.get("role") or "").casefold()
        fields.append(FormField(
            ref=ref, label=label, role=role, value=str(node.get("value") or ""),
            required=bool(node.get("required")),
            selected=node.get("selected") if isinstance(node.get("selected"), bool) else None,
            options=tuple(str(item) for item in (node.get("options") or ())),
            document_hint=document_hint(label, role),
            input_type=str(node.get("type") or "").casefold(),
        ))
    return fields


def inspect_question_groups(nodes: list[dict[str, Any]]) -> list[QuestionGroup]:
    """Extract radio/checkbox options and their selected state without guessing."""
    groups: list[QuestionGroup] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def descendants(index: int) -> Iterable[dict[str, Any]]:
        for child in nodes[index].get("children") or []:
            yield nodes[child]
            yield from descendants(child)

    for index, node in enumerate(nodes):
        role = str(node.get("role") or "").casefold()
        label = str(node.get("label") or "").strip()
        if role not in {"radiogroup", "group", "fieldset", "list"} or not label:
            continue
        options = tuple(
            QuestionOption(str(item["ref"]), str(item.get("label") or "").strip(), item.get("selected"))
            for item in descendants(index)
            if item.get("ref") and str(item.get("role") or "").casefold() in {"radio", "checkbox", "button"}
            and str(item.get("label") or "").strip()
        )
        key = (label.casefold(), tuple(option.ref for option in options))
        if len(options) >= 2 and key not in seen:
            seen.add(key)
            groups.append(QuestionGroup(label, role, options, bool(node.get("required"))))
    return groups
