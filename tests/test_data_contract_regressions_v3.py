from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_llm_gateway_rejects_non_object_provider_json(monkeypatch):
    from services.common import llm_gateway as gateway

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'["not", "an", "object"]'

    monkeypatch.setattr(gateway.urllib.request, "urlopen", lambda *_a, **_k: Response())
    with pytest.raises(gateway.LLMGatewayError, match="expected a JSON object"):
        gateway._post_json("https://provider.invalid/v1/chat", {}, timeout=1)


def test_llm_gateway_nested_chat_shape_drift_returns_no_content_not_attribute_error():
    from services.common import llm_gateway as gateway

    assert gateway._chat_content({"choices": "bad"}, ollama=False) is None
    assert gateway._chat_content({"choices": ["bad"]}, ollama=False) is None
    assert gateway._chat_content({"choices": [{"message": "bad"}]}, ollama=False) is None
    assert gateway._chat_content({"message": "bad"}, ollama=True) is None
    assert gateway._chat_content({"message": {"content": "ok"}}, ollama=True) == "ok"


def test_telegram_api_requires_object_and_update_helpers_filter_bad_items(monkeypatch):
    telegram = _load("audit_telegram_shape_v3", "services/telegram/telegram_review_bot_v1.py")

    class Response:
        ok = True
        text = "[]"

        def json(self):
            return []

    monkeypatch.setattr(telegram.requests, "post", lambda *_a, **_k: Response())
    with pytest.raises(telegram.TelegramError, match="expected a JSON object"):
        telegram.api("token", "getUpdates", timeout=1)

    assert telegram._result_list({"result": "bad"}) == []
    assert telegram._result_list({"result": [{"update_id": 1}, "bad", None]}) == [{"update_id": 1}]
    assert telegram._result_dict({"result": ["bad"]}) == {}


def test_repo_inventory_normalizes_external_boolean_and_topics_shapes(monkeypatch):
    inventory = _load("audit_repo_inventory_v3", "services/repo-audit/repo_inventory_v1.py")

    monkeypatch.setattr(inventory, "head_sha", lambda *_a, **_k: "a" * 40)
    monkeypatch.setattr(inventory, "get_json", lambda *_a, **_k: [{
        "full_name": "acme/repo",
        "clone_url": "https://github.com/acme/repo.git",
        "html_url": "https://github.com/acme/repo",
        "default_branch": "main",
        "private": "false",
        "fork": "0",
        "archived": "no",
        "topics": "security",
    }, "malformed-item"])
    rows = inventory.inventory(github_user="acme", token=None)
    assert len(rows) == 1
    assert rows[0]["private"] is False
    assert rows[0]["fork"] is False
    assert rows[0]["archived"] is False
    assert rows[0]["topics"] == []

    monkeypatch.setattr(inventory, "get_json", lambda *_a, **_k: {"message": "shape drift"})
    with pytest.raises(ValueError, match="must be a JSON list"):
        inventory.inventory(github_user="acme", token=None)


def test_repository_evidence_manifest_does_not_character_split_topics_or_truthy_false_strings():
    evidence = _load("audit_repository_evidence_v3", "services/repo-audit/repository_evidence_v1.py")
    rows = evidence.inventory_records({"repos": [{
        "full_name": "acme/repo",
        "html_url": "https://github.com/acme/repo",
        "private": "false",
        "fork": "false",
        "archived": "0",
        "topics": "security",
    }]})
    assert len(rows) == 1
    assert rows[0]["is_private"] is False
    assert rows[0]["is_fork"] is False
    assert rows[0]["archived"] is False
    assert rows[0]["topics"] == []
    assert evidence.unique_terms(["security", "Security", "python"]) == ["security", "python"]


def test_repository_freshness_normalizes_github_nested_shapes_and_boolean_strings(monkeypatch):
    freshness = _load("audit_repository_freshness_v3", "services/repo-audit/repository_freshness_v1.py")
    payloads = iter([
        {
            "default_branch": "main",
            "html_url": "https://github.com/acme/repo",
            "clone_url": "https://github.com/acme/repo.git",
            "private": "false",
            "fork": "0",
            "archived": "no",
            "topics": "security",
        },
        {"sha": "b" * 40, "commit": {"tree": "malformed"}},
    ])
    monkeypatch.setattr(freshness, "github_json", lambda *_a, **_k: next(payloads))
    state = freshness.github_repository_state("acme/repo", "", None)
    assert state["private"] is False
    assert state["fork"] is False
    assert state["archived"] is False
    assert state["topics"] == []
    assert state["tree_sha"] is None

    monkeypatch.setattr(freshness, "github_json", lambda *_a, **_k: [])
    with pytest.raises(freshness.RefreshError, match="not a JSON object"):
        freshness.github_repository_state("acme/repo", "main", None)


def test_batch_safe_upload_requires_strict_delegation_boolean():
    ux = _load("audit_ux_bool_v3", "services/review/ux_policy_v1.py")
    base = {"approval_type": "privileged_upload_document"}
    assert ux.is_batch_safe_item(item_type="approval_request", payload={**base, "delegated_to_autofill": "false"}) is False
    assert ux.is_batch_safe_item(item_type="approval_request", payload={**base, "delegated_to_autofill": "true"}) is False
    assert ux.is_batch_safe_item(item_type="approval_request", payload={**base, "delegated_to_autofill": True}) is True
    assert ux.is_batch_safe_item(item_type="approval_request", payload={**base, "delegated_to_autofill": False}) is False


def test_form_inspector_strictly_coerces_required_and_ignores_malformed_options():
    inspector = _load("audit_form_inspector_v3", "services/autofill/form_inspector_v1.py")
    fields = inspector.inspect_nodes([
        {"ref": "email", "label": "Email", "role": "textbox", "required": "false", "options": {"bad": 1}},
        "bad-node",
    ])
    assert len(fields) == 1
    assert fields[0].required is False
    assert fields[0].options == ()


def test_market_requirement_parser_rejects_top_level_array_even_when_fenced():
    market = _load("audit_market_json_v3", "services/discovery/market_demand_intelligence_v1.py")
    with pytest.raises(ValueError, match="must be an object|JSON object"):
        market.clean_json_object('```json\n[{"requirements": []}]\n```')


def test_telegram_string_false_ok_and_malformed_nested_updates_do_not_gain_success_or_crash(monkeypatch):
    telegram = _load("audit_telegram_nested_v3", "services/telegram/telegram_review_bot_v1.py")

    class FalseResponse:
        ok = True
        text = '{"ok":"false"}'
        def json(self):
            return {"ok": "false", "description": "nope"}

    monkeypatch.setattr(telegram.requests, "post", lambda *_a, **_k: FalseResponse())
    with pytest.raises(telegram.TelegramError, match="failed"):
        telegram.api("token", "getUpdates", timeout=1)
    assert telegram._safe_int({"bad": 1}) == 0


def test_embedding_vector_contract_rejects_string_boolean_and_non_finite_values():
    from services.common import llm_gateway as gateway

    assert gateway._embedding_vector([1, 2.5, 0]) == [1.0, 2.5, 0.0]
    assert gateway._embedding_vector(["1.0"]) is None
    assert gateway._embedding_vector([True]) is None
    assert gateway._embedding_vector([float("nan")]) is None
    assert gateway._embedding_vector([]) is None
