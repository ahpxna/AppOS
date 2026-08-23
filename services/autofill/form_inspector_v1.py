"""Normalize an ATS accessibility snapshot into transport-neutral form fields.

The inspector does not read profile data and does not perform browser actions.
Adapters (OpenClaw, Playwright, CDP) may supply snapshots in their own shape,
then map them to ``FormField`` before the safety pipeline runs.
"""
from __future__ import annotations

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


def inspect_nodes(nodes: Iterable[dict[str, Any]]) -> list[FormField]:
    """Build fields from the existing OpenClaw snapshot parser's node format."""
    fields: list[FormField] = []
    for node in nodes:
        ref, label = str(node.get("ref") or ""), str(node.get("label") or "").strip()
        if not ref or not label:
            continue
        fields.append(FormField(
            ref=ref, label=label, role=str(node.get("role") or "").casefold(),
            value=str(node.get("value") or ""), required=bool(node.get("required")),
            selected=node.get("selected") if isinstance(node.get("selected"), bool) else None,
            options=tuple(str(item) for item in (node.get("options") or ())),
            document_hint=node.get("document_hint"),
        ))
    return fields
