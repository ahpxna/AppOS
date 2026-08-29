"""Deterministic helpers for LLM/API contract boundaries.

The rest of AppOS should receive ordinary Python values, not provider-specific
JSON presentation quirks. This module intentionally uses only the standard
library so it cannot become another runtime/control plane.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def strip_model_thinking(text: str) -> str:
    """Remove presentation-only reasoning wrappers emitted by some local models."""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # Some local models return only the tail of a truncated thinking block.
    cleaned = re.sub(r"^.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _prose_objects(text: str) -> list[dict[str, Any]]:
    """Decode non-overlapping JSON objects embedded in surrounding prose."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        index = start + max(consumed, 1)
    return objects


def parse_json_object(
    raw: str,
    *,
    preprocess: Callable[[str], str] | None = None,
    error_message: str = "Model output JSON must be an object.",
    prefer_last: bool = False,
) -> dict[str, Any]:
    """Parse one unambiguous JSON object from model/provider response text.

    Exact JSON is preferred. Markdown fences and a short prose wrapper are
    tolerated because multiple supported providers emit them despite a JSON
    instruction. Top-level arrays/scalars and ambiguous multiple objects fail
    closed. ``prefer_last`` exists only for the legacy structured-asset auditor,
    whose previous parser deliberately selected the last complete object.
    """
    if not isinstance(raw, str):
        raise ValueError(error_message)
    text = preprocess(raw) if preprocess is not None else raw
    if not isinstance(text, str):
        raise ValueError(error_message)
    text = text.strip()

    # Exact valid JSON is data, not presentation.  Parse it before touching
    # model-specific ``<think>`` wrappers so literal strings containing those
    # markers are never deleted or truncated.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        raise ValueError(error_message)

    text = strip_model_thinking(text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        raise ValueError(error_message)

    fenced: list[dict[str, Any]] = []
    saw_non_object_fence = False
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            candidate = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            fenced.append(candidate)
        else:
            saw_non_object_fence = True
    if fenced:
        if len(fenced) == 1:
            return fenced[0]
        if prefer_last:
            return fenced[-1]
        raise ValueError(error_message)
    if saw_non_object_fence:
        raise ValueError(error_message)

    objects = _prose_objects(text)
    if len(objects) == 1:
        return objects[0]
    if objects and prefer_last:
        return objects[-1]
    raise ValueError(error_message)
