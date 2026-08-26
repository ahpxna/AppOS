"""Unit tests for backend selection without calling an LLM service."""

import pytest

from services.common import llm_gateway
from services.common import llm_cost_accounting_v1 as cost_accounting


@pytest.fixture
def no_cost_db(monkeypatch):
    reservation = cost_accounting.Reservation("00000000-0000-0000-0000-000000000001", 0, "unit", "test")
    monkeypatch.setattr(cost_accounting, "reserve_paid_call", lambda **_kwargs: reservation)
    monkeypatch.setattr(cost_accounting, "settle_paid_call", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cost_accounting, "mark_paid_call_uncertain", lambda *_args, **_kwargs: None)


def test_resolve_config_uses_local_backend_by_default(monkeypatch):
    monkeypatch.delenv("JOBOS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("JOBOS_DOCGEN_LLM_BACKEND", raising=False)
    config = llm_gateway.resolve_config(
        role="docgen", model="unit-local-model", local_url="http://local.test:11434"
    )

    assert config.backend == "ollama"
    assert config.model == "unit-local-model"
    assert config.base_url == "http://local.test:11434"
    assert config.api_key is None


def test_role_api_override_uses_openai_compatible_transport(monkeypatch, no_cost_db):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("JOBOS_DOCGEN_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_DOCGEN_LLM_API_BASE", "https://api.example.test/v1")
    monkeypatch.setenv("JOBOS_DOCGEN_LLM_API_KEY", "unit-test-token")
    monkeypatch.setenv("JOBOS_DOCGEN_API_MODEL", "unit-api-model")
    calls = []

    def fake_post(url, payload, *, timeout, api_key=None):
        calls.append((url, payload, timeout, api_key))
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    monkeypatch.setattr(llm_gateway, "_post_json", fake_post)
    result = llm_gateway.chat_text(
        role="docgen",
        messages=[{"role": "user", "content": "return json"}],
        json_mode=True,
        timeout=9,
    )

    assert result == '{"ok": true}'
    assert calls == [
        (
            "https://api.example.test/v1/chat/completions",
            {
                "model": "unit-api-model",
                "messages": [{"role": "user", "content": "return json"}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "max_tokens": 4096,
            },
            9,
            "unit-test-token",
        )
    ]


def test_embeddings_use_api_backend_and_preserve_input_order(monkeypatch, no_cost_db):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://embed.example.test")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")
    monkeypatch.setenv("JOBOS_LLM_API_MODEL", "unit-embed-model")
    calls = []

    def fake_post(url, payload, *, timeout, api_key=None):
        calls.append((url, payload, timeout, api_key))
        return {"data": [{"embedding": [1, 2]}, {"embedding": [3, 4]}]}

    monkeypatch.setattr(llm_gateway, "_post_json", fake_post)
    vectors = llm_gateway.embed_texts(texts=["first", "second"], timeout=7)

    assert vectors == [[1, 2], [3, 4]]
    assert calls[0][0] == "https://embed.example.test/v1/embeddings"
    assert calls[0][1]["input"] == ["first", "second"]


def test_deepseek_style_keeps_its_documented_base_url_shape(monkeypatch, no_cost_db):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_STYLE", "deepseek")
    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://api.deepseek.example")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")
    monkeypatch.setenv("JOBOS_LLM_API_MODEL", "deepseek-v4-flash")
    calls = []

    def fake_post(url, payload, *, timeout, api_key=None):
        calls.append((url, payload, api_key))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(llm_gateway, "_post_json", fake_post)
    assert llm_gateway.generate_text(role="job_fit", prompt="unit", timeout=7) == "ok"
    assert calls[0][0] == "https://api.deepseek.example/chat/completions"
    assert calls[0][2] == "unit-test-token"


def test_deepseek_style_is_rejected_for_embeddings(monkeypatch):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_STYLE", "deepseek")
    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://api.deepseek.example")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")

    with pytest.raises(llm_gateway.LLMGatewayError, match="embedding-capable"):
        llm_gateway.embed_texts(texts=["unit"])
