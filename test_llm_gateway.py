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


def test_openai_compatible_endpoint_preserves_known_complete_api_roots():
    assert llm_gateway._api_endpoint(
        "https://api.groq.com/openai/v1", "chat/completions"
    ) == "https://api.groq.com/openai/v1/chat/completions"
    assert llm_gateway._api_endpoint(
        "https://generativelanguage.googleapis.com/v1beta/openai", "chat/completions"
    ) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    # Preserve historical behavior for custom proxy subpaths that are not an
    # explicitly complete OpenAI-compatible root.
    assert llm_gateway._api_endpoint(
        "https://proxy.example.test/api", "chat/completions"
    ) == "https://proxy.example.test/api/v1/chat/completions"


def test_api_backend_rejects_insecure_or_secret_bearing_base_urls(monkeypatch):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")

    monkeypatch.setenv("JOBOS_LLM_API_BASE", "http://api.example.test/v1")
    with pytest.raises(llm_gateway.LLMGatewayError, match="requires HTTPS"):
        llm_gateway.resolve_config(role="docgen", model="unit")

    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://user:secret@api.example.test/v1")
    with pytest.raises(llm_gateway.LLMGatewayError, match="must not contain credentials"):
        llm_gateway.resolve_config(role="docgen", model="unit")

    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://api.example.test/v1?token=secret")
    with pytest.raises(llm_gateway.LLMGatewayError, match="must not contain credentials"):
        llm_gateway.resolve_config(role="docgen", model="unit")

    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://api.anthropic.com")
    with pytest.raises(llm_gateway.LLMGatewayError, match="Native Anthropic Messages API is not implemented"):
        llm_gateway.resolve_config(role="docgen", model="unit")

    monkeypatch.setenv("JOBOS_LLM_API_BASE", "http://127.0.0.1:9000/v1")
    assert llm_gateway.resolve_config(role="docgen", model="unit").base_url == "http://127.0.0.1:9000/v1"


def test_embedding_result_records_configured_and_resolved_provider_identity(monkeypatch, no_cost_db):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://generativelanguage.googleapis.com/v1beta/openai")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")
    monkeypatch.setenv("JOBOS_LLM_API_MODEL", "configured-embed-model")
    calls = []

    def fake_post(url, payload, *, timeout, api_key=None):
        calls.append((url, payload, timeout, api_key))
        return {
            "id": "embed-request-1",
            "model": "resolved-embed-model",
            "usage": {"prompt_tokens": 7},
            "data": [{"embedding": [0.25, 0.75]}],
        }

    monkeypatch.setattr(llm_gateway, "_post_json", fake_post)
    result = llm_gateway.embed_result(texts=["hello"], timeout=4)

    assert result.vectors == [[0.25, 0.75]]
    assert result.provider == "gemini"
    assert result.configured_model == "configured-embed-model"
    assert result.model == "resolved-embed-model"
    assert result.tokens_input == 7
    assert result.request_id == "embed-request-1"
    assert calls[0][0] == "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"


def test_embedding_inputs_reject_scalar_string(monkeypatch):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "ollama")
    with pytest.raises(llm_gateway.LLMGatewayError, match="sequence of strings"):
        llm_gateway.embed_result(texts="not-a-batch")


def test_embeddings_reorder_indexed_api_rows_to_input_identity(monkeypatch, no_cost_db):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://embed.example.test/v1")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")
    monkeypatch.setenv("JOBOS_LLM_API_MODEL", "unit-embed-model")

    monkeypatch.setattr(
        llm_gateway,
        "_post_json",
        lambda *_args, **_kwargs: {
            "data": [
                {"index": 1, "embedding": [20, 21]},
                {"index": 0, "embedding": [10, 11]},
            ]
        },
    )

    assert llm_gateway.embed_texts(texts=["first", "second"]) == [
        [10.0, 11.0],
        [20.0, 21.0],
    ]


def test_embeddings_fail_closed_on_partial_or_duplicate_indexes(monkeypatch, no_cost_db):
    monkeypatch.setenv("JOBOS_LLM_BACKEND", "api")
    monkeypatch.setenv("JOBOS_LLM_API_BASE", "https://embed.example.test/v1")
    monkeypatch.setenv("JOBOS_LLM_API_KEY", "unit-test-token")
    monkeypatch.setenv("JOBOS_LLM_API_MODEL", "unit-embed-model")

    for payload, message in (
        ({"data": [{"index": 0, "embedding": [1]}, {"embedding": [2]}]}, "mixed indexed"),
        ({"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}]}, "duplicate"),
    ):
        monkeypatch.setattr(llm_gateway, "_post_json", lambda *_args, _payload=payload, **_kwargs: _payload)
        with pytest.raises(llm_gateway.LLMGatewayError, match=message):
            llm_gateway.embed_texts(texts=["first", "second"])


def test_post_json_normalizes_malformed_http_200_json(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return b"not-json"

    monkeypatch.setattr(llm_gateway.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    with pytest.raises(llm_gateway.LLMGatewayError, match="malformed JSON"):
        llm_gateway._post_json("https://api.example.test/v1/test", {}, timeout=1)
