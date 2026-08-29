from __future__ import annotations

import pytest

from services.common.ai_contracts import parse_json_object


def test_parse_json_object_accepts_supported_model_wrappers():
    assert parse_json_object('{"ok": true}') == {"ok": True}
    assert parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_object('<think>private reasoning</think>\n{"ok": true}') == {"ok": True}
    assert parse_json_object('Result follows: {"ok": true}\nDone.') == {"ok": True}


def test_parse_json_object_rejects_top_level_non_objects():
    for raw in ('[]', '```json\n[]\n```', '"text"', 'true'):
        with pytest.raises(ValueError, match="must be an object"):
            parse_json_object(raw)


def test_parse_json_object_rejects_ambiguous_multiple_objects():
    with pytest.raises(ValueError, match="must be an object"):
        parse_json_object('first {"a": 1} then {"b": 2}')
    assert parse_json_object('first {"a": 1} then {"b": 2}', prefer_last=True) == {"b": 2}


def test_parse_json_object_handles_braces_inside_json_strings():
    assert parse_json_object('prefix {"text": "literal } and { braces"} suffix') == {
        "text": "literal } and { braces"
    }


def test_parse_json_object_preserves_literal_think_markers_inside_valid_json():
    assert parse_json_object('{"text":"literal <think>not reasoning</think> marker","ok":true}') == {
        "text": "literal <think>not reasoning</think> marker",
        "ok": True,
    }
    assert parse_json_object('{"text":"literal </think> marker","ok":true}') == {
        "text": "literal </think> marker",
        "ok": True,
    }
