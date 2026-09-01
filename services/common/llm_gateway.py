"""One transport layer for local Ollama and OpenAI-compatible token APIs.

All text-generation services should call this module rather than hard-coding an
Ollama endpoint. The backend is selected globally or per role using environment
variables, while model selection remains centralised in ``model_config.py``.

Configuration:
  JOBOS_LLM_BACKEND=ollama|api                  (default: ollama)
  JOBOS_<ROLE>_LLM_BACKEND=ollama|api           (optional override)
  OLLAMA_URL=http://127.0.0.1:11434             (local backend)
  JOBOS_LLM_API_BASE=https://.../v1             (API backend)
  JOBOS_LLM_API_KEY=...                         (API backend, never commit)
  JOBOS_LLM_API_MODEL=...                       (optional API model default)
  JOBOS_LLM_API_STYLE=openai|deepseek           (default: openai)
  JOBOS_<ROLE>_API_MODEL=...                    (optional API model override)

``api`` uses OpenAI-compatible chat/completions and embeddings interfaces.
The ``deepseek`` style preserves DeepSeek's root-base URL convention. Other
compatible providers may publish an explicit API root (for example ``/openai/v1``
or Gemini's ``/v1beta/openai``); the gateway appends resources without inserting
a second ``/v1``. Use an embedding-capable provider for the ``embed`` role when
the chat provider lacks embeddings.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Sequence
from urllib.parse import urlsplit

from .model_config import get_model
from .config import env_int


class LLMGatewayError(RuntimeError):
    """A backend/configuration failure that callers should report clearly."""


@dataclass(frozen=True)
class LLMConfig:
    role: str
    backend: str
    model: str
    base_url: str
    api_key: str | None
    api_style: str = "openai"
    provider: str = "unknown"


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    tokens_input: int
    tokens_output: int
    estimated_cost_usd: float
    request_id: str | None = None


@dataclass(frozen=True)
class LLMEmbeddingResult:
    vectors: list[list[float]]
    provider: str
    configured_model: str
    model: str
    tokens_input: int
    estimated_cost_usd: float
    request_id: str | None = None


def _role_prefix(role: str) -> str:
    return role.upper().replace("-", "_")



def _provider_name(base_url: str, api_style: str, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip().casefold()
    host = (urlsplit(base_url).hostname or "").casefold()
    if "openrouter" in host:
        return "openrouter"
    if "openai" in host:
        return "openai"
    if "deepseek" in host:
        return "deepseek"
    if "anthropic" in host:
        return "anthropic"
    if "generativelanguage.googleapis.com" in host:
        return "gemini"
    if "groq.com" in host:
        return "groq"
    if "together" in host:
        return "together"
    if "huggingface" in host:
        return "huggingface"
    return api_style


def _validate_api_base(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMGatewayError("JOBOS_LLM_API_BASE must be an absolute http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LLMGatewayError(
            "JOBOS_LLM_API_BASE must not contain credentials, query parameters, or fragments."
        )
    host = parsed.hostname.casefold()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise LLMGatewayError(
            "Paid/token API traffic requires HTTPS unless the API endpoint is loopback-local."
        )
    if host == "api.anthropic.com" or host.endswith(".api.anthropic.com"):
        raise LLMGatewayError(
            "Native Anthropic Messages API is not implemented by this OpenAI-compatible gateway; "
            "use a tested compatible gateway/provider instead of silently sending the wrong schema."
        )


def resolve_config(*, role: str, model: str | None = None,
                   local_url: str | None = None) -> LLMConfig:
    """Resolve one role's local-or-token-API configuration without exposing its key.

    Implementation note: this is the compatibility seam added for the
    pipeline. Callers keep their role/model choice, while deployment selects
    Ollama or an OpenAI-compatible token endpoint through environment values.
    """
    prefix = _role_prefix(role)
    backend = (os.getenv(f"JOBOS_{prefix}_LLM_BACKEND") or
               os.getenv("JOBOS_LLM_BACKEND", "ollama")).strip().lower()
    if backend not in {"ollama", "api"}:
        raise LLMGatewayError(
            f"Unsupported backend {backend!r}; use 'ollama' or 'api'."
        )
    if backend == "ollama":
        return LLMConfig(
            role=role, backend=backend, model=model or get_model(role),
            base_url=(local_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/"),
            api_key=None, api_style="ollama", provider="ollama",
        )
    base_url = (os.getenv(f"JOBOS_{prefix}_LLM_API_BASE") or
                os.getenv("JOBOS_LLM_API_BASE", "")).rstrip("/")
    api_key = (os.getenv(f"JOBOS_{prefix}_LLM_API_KEY") or
               os.getenv("JOBOS_LLM_API_KEY"))
    api_model = (os.getenv(f"JOBOS_{prefix}_API_MODEL") or
                 os.getenv("JOBOS_LLM_API_MODEL") or model or get_model(role))
    api_style = (os.getenv(f"JOBOS_{prefix}_LLM_API_STYLE") or
                 os.getenv("JOBOS_LLM_API_STYLE", "openai")).strip().lower()
    if api_style not in {"openai", "deepseek"}:
        raise LLMGatewayError(
            f"Unsupported API style {api_style!r}; use 'openai' or 'deepseek'."
        )
    if not base_url:
        raise LLMGatewayError(
            "API backend selected but JOBOS_LLM_API_BASE is not set. "
            "Use an OpenAI-compatible base URL, typically ending in /v1."
        )
    if not api_key:
        raise LLMGatewayError(
            "API backend selected but JOBOS_LLM_API_KEY is not set. "
            "Put the token in your untracked .env or shell, never in git."
        )
    _validate_api_base(base_url)
    provider = _provider_name(
        base_url, api_style,
        os.getenv(f"JOBOS_{prefix}_LLM_API_PROVIDER") or os.getenv("JOBOS_LLM_API_PROVIDER"),
    )
    return LLMConfig(role=role, backend=backend, model=api_model,
                     base_url=base_url, api_key=api_key, api_style=api_style, provider=provider)


def _api_endpoint(base_url: str, resource: str, *, style: str = "openai") -> str:
    """Append a resource to the configured provider API root exactly once.

    OpenAI-compatible bases ending in ``/v1`` are already complete. Gemini's
    compatibility root ends in ``/v1beta/openai`` and is also complete. Other
    bases keep the historical behavior of receiving ``/v1`` so existing custom
    proxy configurations are not silently reinterpreted. DeepSeek keeps its
    root-base convention.
    """
    root = base_url.rstrip("/")
    if style == "deepseek":
        if root.endswith("/v1"):
            root = root[:-3]
        return root + "/" + resource
    path = urlsplit(root).path.rstrip("/")
    if path.endswith("/v1") or path.endswith("/v1beta/openai"):
        return root + "/" + resource
    return root + "/v1/" + resource


def _post_json(url: str, payload: dict[str, Any], *, timeout: int,
               api_key: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                preview = body[:500].replace("\n", " ")
                raise LLMGatewayError(
                    f"LLM endpoint {url} returned malformed JSON: {preview}"
                ) from exc
            if not isinstance(parsed, dict):
                raise LLMGatewayError(
                    f"LLM endpoint {url} returned {type(parsed).__name__}; expected a JSON object."
                )
            return parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMGatewayError(f"LLM HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMGatewayError(f"LLM request failed for {url}: {exc}") from exc


def _chat_content(response: dict[str, Any], *, ollama: bool) -> str | None:
    """Extract chat text without trusting provider nested response shapes."""
    if ollama:
        message = response.get("message")
        return message.get("content") if isinstance(message, dict) else None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    return message.get("content") if isinstance(message, dict) else None

def _estimate_tokens(text: str) -> int:
    # Conservative portable fallback when provider usage is unavailable.
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def _usage(response: dict[str, Any], *, fallback_input: int, fallback_output_text: str = "") -> tuple[int, int]:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    inp = usage.get("prompt_tokens", response.get("prompt_eval_count"))
    out = usage.get("completion_tokens", response.get("eval_count"))
    try:
        input_tokens = int(inp) if inp is not None else fallback_input
    except (TypeError, ValueError):
        input_tokens = fallback_input
    try:
        output_tokens = int(out) if out is not None else _estimate_tokens(fallback_output_text)
    except (TypeError, ValueError):
        output_tokens = _estimate_tokens(fallback_output_text)
    return max(0, input_tokens), max(0, output_tokens)


def _max_output_tokens() -> int:
    return env_int("JOBOS_LLM_MAX_OUTPUT_TOKENS", 4096, minimum=1, maximum=1_000_000)


def _sha_payload(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def generate_result(*, role: str, prompt: str, model: str | None = None,
                    local_url: str | None = None, timeout: int = 300,
                    temperature: float = 0.2, num_ctx: int | None = None) -> LLMResult:
    config = resolve_config(role=role, model=model, local_url=local_url)
    request_sha256 = _sha_payload({"kind": "generate", "role": role, "provider": config.provider,
                                   "model": config.model, "prompt": prompt, "temperature": temperature,
                                   "num_ctx": num_ctx, "max_output_tokens": _max_output_tokens()})
    input_est = _estimate_tokens(prompt)
    reservation = None
    if config.backend == "api":
        from .llm_cost_accounting_v1 import reserve_paid_call
        reservation = reserve_paid_call(
            role=role, provider=config.provider, model=config.model,
            estimated_input_tokens=input_est, max_output_tokens=_max_output_tokens(),
            request_sha256=request_sha256, request_kind='generate',
        )
        if reservation.cached_response_json is not None:
            cached_text = reservation.cached_response_json.get("text")
            if not isinstance(cached_text, str) or not cached_text.strip():
                raise LLMGatewayError("Cached exact generate response is invalid; refusing provider replay.")
            return LLMResult(
                text=cached_text, provider=config.provider,
                model=reservation.cached_resolved_model or config.model,
                tokens_input=reservation.cached_input_tokens,
                tokens_output=reservation.cached_output_tokens,
                estimated_cost_usd=float(reservation.cached_cost_usd),
                request_id=reservation.cached_request_id,
            )
    else:
        from .llm_cost_accounting_v1 import lookup_completed_call
        cached = lookup_completed_call(
            role=role, provider=config.provider, model=config.model,
            request_kind="generate", request_sha256=request_sha256,
        )
        if cached is not None:
            cached_text = cached.response_json.get("text")
            if isinstance(cached_text, str) and cached_text.strip():
                return LLMResult(
                    text=cached_text, provider=config.provider,
                    model=cached.resolved_model or config.model,
                    tokens_input=cached.input_tokens, tokens_output=cached.output_tokens,
                    estimated_cost_usd=float(cached.cost_usd), request_id=cached.request_id,
                )
    try:
        if config.backend == "ollama":
            options: dict[str, Any] = {"temperature": temperature}
            if num_ctx:
                options["num_ctx"] = num_ctx
            payload = {"model": config.model, "prompt": prompt, "stream": False, "options": options}
            response = _post_json(config.base_url + "/api/generate", payload, timeout=timeout)
            text = response.get("response")
        else:
            payload = {
                "model": config.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": _max_output_tokens(),
            }
            response = _post_json(_api_endpoint(config.base_url, "chat/completions", style=config.api_style), payload,
                                  timeout=timeout, api_key=config.api_key)
            text = _chat_content(response, ollama=False)
    except Exception as exc:
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(reservation, role=role, configured_model=config.model,
                                     estimated_input_tokens=input_est, error=str(exc))
        raise
    if not isinstance(text, str) or not text.strip():
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(reservation, role=role, configured_model=config.model,
                                     estimated_input_tokens=input_est, error="provider returned no text")
        raise LLMGatewayError(f"{config.backend} backend returned no text for role={role!r}.")
    input_tokens, output_tokens = _usage(response, fallback_input=input_est, fallback_output_text=text)
    resolved_model = str(response.get("model") or config.model)
    request_id = str(response.get("id")) if response.get("id") else None
    cost = 0.0
    if reservation is not None:
        from .llm_cost_accounting_v1 import settle_paid_call
        cost = float(settle_paid_call(reservation, role=role, configured_model=config.model,
                                      resolved_model=resolved_model, input_tokens=input_tokens,
                                      output_tokens=output_tokens, request_id=request_id,
                                      response_sha256=_sha_payload({"text": text}),
                                      response_json={"text": text}))
    else:
        from .llm_cost_accounting_v1 import record_local_call
        record_local_call(role=role, provider="ollama", model=resolved_model,
                          input_tokens=input_tokens, output_tokens=output_tokens, request_id=request_id,
                          request_sha256=request_sha256, response_sha256=_sha_payload({"text": text}),
                          request_kind='generate', response_json={"text": text})
    return LLMResult(text=text, provider=config.provider,
                     model=resolved_model, tokens_input=input_tokens, tokens_output=output_tokens,
                     estimated_cost_usd=cost, request_id=request_id)


def generate_text(*, role: str, prompt: str, model: str | None = None,
                  local_url: str | None = None, timeout: int = 300,
                  temperature: float = 0.2, num_ctx: int | None = None) -> str:
    """Backward-compatible text API; accounting is performed by generate_result."""
    return generate_result(role=role, prompt=prompt, model=model, local_url=local_url,
                           timeout=timeout, temperature=temperature, num_ctx=num_ctx).text


def chat_result(*, role: str, messages: Sequence[dict[str, str]], model: str | None = None,
                local_url: str | None = None, timeout: int = 300,
                temperature: float = 0.2, num_ctx: int | None = None,
                json_mode: bool = False, json_schema: dict[str, Any] | None = None) -> LLMResult:
    config = resolve_config(role=role, model=model, local_url=local_url)
    request_sha256 = _sha_payload({"kind": "chat", "role": role, "provider": config.provider,
                                   "model": config.model, "messages": list(messages),
                                   "temperature": temperature, "num_ctx": num_ctx,
                                   "json_mode": json_mode, "json_schema": json_schema,
                                   "max_output_tokens": _max_output_tokens()})
    input_est = _estimate_tokens(json.dumps(list(messages), ensure_ascii=False))
    reservation = None
    if config.backend == "api":
        from .llm_cost_accounting_v1 import reserve_paid_call
        reservation = reserve_paid_call(role=role, provider=config.provider, model=config.model,
                                        estimated_input_tokens=input_est, max_output_tokens=_max_output_tokens(),
                                        request_sha256=request_sha256, request_kind='chat')
        if reservation.cached_response_json is not None:
            cached_text = reservation.cached_response_json.get("text")
            if not isinstance(cached_text, str) or not cached_text.strip():
                raise LLMGatewayError("Cached exact chat response is invalid; refusing provider replay.")
            return LLMResult(
                text=cached_text, provider=config.provider,
                model=reservation.cached_resolved_model or config.model,
                tokens_input=reservation.cached_input_tokens,
                tokens_output=reservation.cached_output_tokens,
                estimated_cost_usd=float(reservation.cached_cost_usd),
                request_id=reservation.cached_request_id,
            )
    else:
        from .llm_cost_accounting_v1 import lookup_completed_call
        cached = lookup_completed_call(
            role=role, provider=config.provider, model=config.model,
            request_kind="chat", request_sha256=request_sha256,
        )
        if cached is not None:
            cached_text = cached.response_json.get("text")
            if isinstance(cached_text, str) and cached_text.strip():
                return LLMResult(
                    text=cached_text, provider=config.provider,
                    model=cached.resolved_model or config.model,
                    tokens_input=cached.input_tokens, tokens_output=cached.output_tokens,
                    estimated_cost_usd=float(cached.cost_usd), request_id=cached.request_id,
                )
    try:
        if config.backend == "ollama":
            options: dict[str, Any] = {"temperature": temperature}
            if num_ctx:
                options["num_ctx"] = num_ctx
            payload: dict[str, Any] = {"model": config.model, "messages": list(messages),
                                       "stream": False, "options": options}
            if json_schema is not None:
                payload["format"] = json_schema
            elif json_mode:
                payload["format"] = "json"
            response = _post_json(config.base_url + "/api/chat", payload, timeout=timeout)
            text = _chat_content(response, ollama=True)
        else:
            payload = {"model": config.model, "messages": list(messages), "temperature": temperature,
                       "max_tokens": _max_output_tokens()}
            if json_mode or json_schema is not None:
                payload["response_format"] = {"type": "json_object"}
            response = _post_json(_api_endpoint(config.base_url, "chat/completions", style=config.api_style), payload,
                                  timeout=timeout, api_key=config.api_key)
            text = _chat_content(response, ollama=False)
    except Exception as exc:
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(reservation, role=role, configured_model=config.model,
                                     estimated_input_tokens=input_est, error=str(exc))
        raise
    if not isinstance(text, str) or not text.strip():
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(reservation, role=role, configured_model=config.model,
                                     estimated_input_tokens=input_est, error="provider returned no chat text")
        raise LLMGatewayError(f"{config.backend} backend returned no chat text for role={role!r}.")
    input_tokens, output_tokens = _usage(response, fallback_input=input_est, fallback_output_text=text)
    resolved_model = str(response.get("model") or config.model)
    request_id = str(response.get("id")) if response.get("id") else None
    cost = 0.0
    if reservation is not None:
        from .llm_cost_accounting_v1 import settle_paid_call
        cost = float(settle_paid_call(reservation, role=role, configured_model=config.model,
                                      resolved_model=resolved_model, input_tokens=input_tokens,
                                      output_tokens=output_tokens, request_id=request_id,
                                      response_sha256=_sha_payload({"text": text}),
                                      response_json={"text": text}))
    else:
        from .llm_cost_accounting_v1 import record_local_call
        record_local_call(role=role, provider="ollama", model=resolved_model,
                          input_tokens=input_tokens, output_tokens=output_tokens, request_id=request_id,
                          request_sha256=request_sha256, response_sha256=_sha_payload({"text": text}),
                          request_kind='chat', response_json={"text": text})
    return LLMResult(text=text, provider=config.provider,
                     model=resolved_model, tokens_input=input_tokens, tokens_output=output_tokens,
                     estimated_cost_usd=cost, request_id=request_id)


def chat_text(*, role: str, messages: Sequence[dict[str, str]], model: str | None = None,
              local_url: str | None = None, timeout: int = 300,
              temperature: float = 0.2, num_ctx: int | None = None,
              json_mode: bool = False, json_schema: dict[str, Any] | None = None) -> str:
    return chat_result(role=role, messages=messages, model=model, local_url=local_url, timeout=timeout,
                       temperature=temperature, num_ctx=num_ctx, json_mode=json_mode, json_schema=json_schema).text


def _embedding_vector(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        result.append(number)
    return result

def embed_result(*, texts: Sequence[str], model: str | None = None,
                 local_url: str | None = None, timeout: int = 180) -> LLMEmbeddingResult:
    """Embed a batch and return canonical provider/model/accounting metadata."""
    config = resolve_config(role="embed", model=model, local_url=local_url)
    if isinstance(texts, (str, bytes)):
        raise LLMGatewayError("Embedding inputs must be a sequence of strings, not one scalar string.")
    text_list = list(texts)
    if not text_list:
        return LLMEmbeddingResult(
            vectors=[], provider=config.provider, configured_model=config.model, model=config.model,
            tokens_input=0, estimated_cost_usd=0.0, request_id=None,
        )
    if any(not isinstance(text, str) for text in text_list):
        raise LLMGatewayError("Embedding inputs must be strings.")
    if config.backend == "api" and config.api_style == "deepseek":
        raise LLMGatewayError(
            "DeepSeek API style is not an embedding-capable endpoint; configure an embedding-capable provider for role='embed'."
        )
    request_sha256 = _sha_payload({"kind": "embed", "provider": config.provider,
                                   "model": config.model, "texts": text_list})
    input_est = sum(_estimate_tokens(text) for text in text_list)
    reservation = None
    if config.backend == "api":
        from .llm_cost_accounting_v1 import reserve_paid_call
        reservation = reserve_paid_call(role="embed", provider=config.provider, model=config.model,
                                        estimated_input_tokens=input_est, max_output_tokens=0,
                                        request_sha256=request_sha256, request_kind='embed')
        if reservation.cached_response_json is not None:
            cached_vectors = reservation.cached_response_json.get("embeddings")
            normalized_cached = (
                [_embedding_vector(value) for value in cached_vectors]
                if isinstance(cached_vectors, list) else []
            )
            if len(normalized_cached) != len(text_list) or any(value is None for value in normalized_cached):
                raise LLMGatewayError("Cached exact embedding response is invalid; refusing provider replay.")
            return LLMEmbeddingResult(
                vectors=[value for value in normalized_cached if value is not None],
                provider=config.provider, configured_model=config.model,
                model=reservation.cached_resolved_model or config.model,
                tokens_input=reservation.cached_input_tokens,
                estimated_cost_usd=float(reservation.cached_cost_usd),
                request_id=reservation.cached_request_id,
            )
    else:
        from .llm_cost_accounting_v1 import lookup_completed_call
        cached = lookup_completed_call(
            role="embed", provider=config.provider, model=config.model,
            request_kind="embed", request_sha256=request_sha256,
        )
        if cached is not None:
            cached_vectors = cached.response_json.get("embeddings")
            normalized_cached = (
                [_embedding_vector(value) for value in cached_vectors]
                if isinstance(cached_vectors, list) else []
            )
            if len(normalized_cached) == len(text_list) and all(value is not None for value in normalized_cached):
                return LLMEmbeddingResult(
                    vectors=[value for value in normalized_cached if value is not None],
                    provider=config.provider, configured_model=config.model,
                    model=cached.resolved_model or config.model,
                    tokens_input=cached.input_tokens,
                    estimated_cost_usd=float(cached.cost_usd), request_id=cached.request_id,
                )
    try:
        if config.backend == "ollama":
            response = _post_json(
                config.base_url + "/api/embed",
                {"model": config.model, "input": text_list},
                timeout=timeout,
            )
            embeddings = response.get("embeddings")
            if embeddings is None:
                embeddings = []
                for text in text_list:
                    legacy = _post_json(
                        config.base_url + "/api/embeddings",
                        {"model": config.model, "prompt": text},
                        timeout=timeout,
                    )
                    vector = _embedding_vector(legacy.get("embedding"))
                    if vector is None:
                        raise LLMGatewayError("Ollama embedding backend returned no valid vector.")
                    embeddings.append(vector)
        else:
            response = _post_json(
                _api_endpoint(config.base_url, "embeddings", style=config.api_style),
                {"model": config.model, "input": text_list},
                timeout=timeout,
                api_key=config.api_key,
            )
            data = response.get("data")
            if not isinstance(data, list):
                data = []
            # OpenAI-compatible embedding responses carry an explicit ``index``
            # binding each vector to its input.  Providers/proxies are not
            # required to serialize rows in input order, so response order is
            # not an identity.  Preserve legacy compatible providers that omit
            # index entirely, but fail closed on partial/duplicate/bogus index
            # metadata rather than silently attaching a vector to the wrong
            # profile chunk.
            rows_with_index = [
                row for row in data
                if isinstance(row, dict) and row.get("index") is not None
            ]
            if rows_with_index:
                if len(rows_with_index) != len(data):
                    raise LLMGatewayError("Embedding response mixed indexed and unindexed rows.")
                indexed: list[list[float] | None] = [None] * len(text_list)
                for row in data:
                    if not isinstance(row, dict):
                        raise LLMGatewayError("Embedding response row was not an object.")
                    index = row.get("index")
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(text_list):
                        raise LLMGatewayError("Embedding response contained an invalid input index.")
                    if indexed[index] is not None:
                        raise LLMGatewayError("Embedding response contained a duplicate input index.")
                    vector = _embedding_vector(row.get("embedding"))
                    if vector is None:
                        raise LLMGatewayError("Embedding response contained an invalid vector.")
                    indexed[index] = vector
                if any(vector is None for vector in indexed):
                    raise LLMGatewayError("Embedding response omitted one or more input indexes.")
                embeddings = [vector for vector in indexed if vector is not None]
            else:
                embeddings = []
                for row in data:
                    vector = _embedding_vector(row.get("embedding")) if isinstance(row, dict) else None
                    if vector is not None:
                        embeddings.append(vector)
    except Exception as exc:
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(
                reservation, role="embed", configured_model=config.model,
                estimated_input_tokens=input_est, error=str(exc),
            )
        raise
    normalized = [_embedding_vector(value) for value in embeddings] if isinstance(embeddings, list) else []
    if len(normalized) != len(text_list) or any(vector is None for vector in normalized):
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(
                reservation, role="embed", configured_model=config.model,
                estimated_input_tokens=input_est, error="provider returned invalid embedding batch",
            )
        raise LLMGatewayError(f"{config.backend} embedding backend returned an invalid batch.")
    vectors = [vector for vector in normalized if vector is not None]
    input_tokens, _ = _usage(response, fallback_input=input_est, fallback_output_text="")
    resolved_model = str(response.get("model") or config.model)
    request_id = str(response.get("id")) if response.get("id") else None
    cost = 0.0
    if reservation is not None:
        from .llm_cost_accounting_v1 import settle_paid_call
        cost = float(settle_paid_call(
            reservation, role="embed", configured_model=config.model, resolved_model=resolved_model,
            input_tokens=input_tokens, output_tokens=0, request_id=request_id,
            response_sha256=_sha_payload({"embeddings": vectors}),
            response_json={"embeddings": vectors},
        ))
    else:
        from .llm_cost_accounting_v1 import record_local_call
        record_local_call(
            role="embed", provider="ollama", model=resolved_model,
            input_tokens=input_tokens, output_tokens=0, request_id=request_id,
            request_sha256=request_sha256, response_sha256=_sha_payload({"embeddings": vectors}),
            request_kind='embed', response_json={"embeddings": vectors},
        )
    return LLMEmbeddingResult(
        vectors=vectors, provider=config.provider, configured_model=config.model, model=resolved_model,
        tokens_input=input_tokens, estimated_cost_usd=cost, request_id=request_id,
    )


def embed_texts(*, texts: Sequence[str], model: str | None = None,
                local_url: str | None = None, timeout: int = 180) -> list[list[float]]:
    """Backward-compatible vector-only embedding API."""
    return embed_result(texts=texts, model=model, local_url=local_url, timeout=timeout).vectors
