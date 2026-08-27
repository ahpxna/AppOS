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

``api`` uses the OpenAI-compatible chat/completions and embeddings interfaces.
The ``deepseek`` style preserves DeepSeek's documented base URL shape, which
does not append ``/v1``.  Use an embedding-capable provider (for example
OpenAI) for the ``embed`` role when the chat provider lacks embeddings.
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
    return api_style

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
    provider = _provider_name(
        base_url, api_style,
        os.getenv(f"JOBOS_{prefix}_LLM_API_PROVIDER") or os.getenv("JOBOS_LLM_API_PROVIDER"),
    )
    return LLMConfig(role=role, backend=backend, model=api_model,
                     base_url=base_url, api_key=api_key, api_style=api_style, provider=provider)


def _api_endpoint(base_url: str, resource: str, *, style: str = "openai") -> str:
    """Build a provider endpoint without forcing DeepSeek through ``/v1``.

    OpenAI-compatible gateways conventionally expose ``.../v1``. DeepSeek's
    documented OpenAI-compatible base URL is the host root instead, so style
    is explicit rather than guessed from a hostname or a secret-bearing URL.
    """
    root = base_url.rstrip("/")
    if style == "deepseek":
        if root.endswith("/v1"):
            root = root[:-3]
        return root + "/" + resource
    return root + "/" + resource if root.endswith("/v1") else root + "/v1/" + resource


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
            parsed = json.loads(response.read().decode("utf-8", errors="replace"))
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
    return max(1, int(os.getenv("JOBOS_LLM_MAX_OUTPUT_TOKENS", "4096")))


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
                                      response_sha256=_sha_payload({"text": text})))
    else:
        from .llm_cost_accounting_v1 import record_local_call
        record_local_call(role=role, provider="ollama", model=resolved_model,
                          input_tokens=input_tokens, output_tokens=output_tokens, request_id=request_id,
                          request_sha256=request_sha256, response_sha256=_sha_payload({"text": text}),
                          request_kind='generate')
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
                                      response_sha256=_sha_payload({"text": text})))
    else:
        from .llm_cost_accounting_v1 import record_local_call
        record_local_call(role=role, provider="ollama", model=resolved_model,
                          input_tokens=input_tokens, output_tokens=output_tokens, request_id=request_id,
                          request_sha256=request_sha256, response_sha256=_sha_payload({"text": text}),
                          request_kind='chat')
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

def embed_texts(*, texts: Sequence[str], model: str | None = None,
                local_url: str | None = None, timeout: int = 180) -> list[list[float]]:
    """Embed a batch with the same paid-budget/per-call accounting boundary."""
    config = resolve_config(role="embed", model=model, local_url=local_url)
    if not texts:
        return []
    if config.backend == "api" and config.api_style == "deepseek":
        raise LLMGatewayError(
            "DeepSeek API style is not an embedding-capable endpoint; configure an embedding-capable provider for role='embed'."
        )
    request_sha256 = _sha_payload({"kind": "embed", "provider": config.provider,
                                   "model": config.model, "texts": list(texts)})
    input_est = sum(_estimate_tokens(text) for text in texts)
    reservation = None
    if config.backend == "api":
        from .llm_cost_accounting_v1 import reserve_paid_call
        # Embeddings have no completion tokens.
        reservation = reserve_paid_call(role="embed", provider=config.provider, model=config.model,
                                        estimated_input_tokens=input_est, max_output_tokens=0,
                                        request_sha256=request_sha256, request_kind='embed')
    try:
        if config.backend == "ollama":
            response = _post_json(config.base_url + "/api/embed", {"model": config.model, "input": list(texts)}, timeout=timeout)
            embeddings = response.get("embeddings")
            if embeddings is None:
                embeddings = []
                for text in texts:
                    legacy = _post_json(config.base_url + "/api/embeddings", {"model": config.model, "prompt": text}, timeout=timeout)
                    vector = _embedding_vector(legacy.get("embedding"))
                    if vector is None:
                        raise LLMGatewayError("Ollama embedding backend returned no valid vector.")
                    embeddings.append(vector)
        else:
            response = _post_json(_api_endpoint(config.base_url, "embeddings", style=config.api_style),
                                  {"model": config.model, "input": list(texts)}, timeout=timeout, api_key=config.api_key)
            data = response.get("data")
            if not isinstance(data, list):
                data = []
            embeddings = []
            for row in data:
                vector = _embedding_vector(row.get("embedding")) if isinstance(row, dict) else None
                if vector is not None:
                    embeddings.append(vector)
    except Exception as exc:
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(reservation, role="embed", configured_model=config.model,
                                     estimated_input_tokens=input_est, error=str(exc))
        raise
    if len(embeddings) != len(texts) or any(_embedding_vector(v) is None for v in embeddings):
        if reservation is not None:
            from .llm_cost_accounting_v1 import mark_paid_call_uncertain
            mark_paid_call_uncertain(reservation, role="embed", configured_model=config.model,
                                     estimated_input_tokens=input_est, error="provider returned invalid embedding batch")
        raise LLMGatewayError(f"{config.backend} embedding backend returned an invalid batch.")
    input_tokens, _ = _usage(response, fallback_input=input_est, fallback_output_text="")
    resolved_model = str(response.get("model") or config.model)
    request_id = str(response.get("id")) if response.get("id") else None
    if reservation is not None:
        from .llm_cost_accounting_v1 import settle_paid_call
        settle_paid_call(reservation, role="embed", configured_model=config.model, resolved_model=resolved_model,
                         input_tokens=input_tokens, output_tokens=0, request_id=request_id,
                         response_sha256=_sha_payload({"embeddings": embeddings}))
    else:
        from .llm_cost_accounting_v1 import record_local_call
        record_local_call(role="embed", provider="ollama", model=resolved_model,
                          input_tokens=input_tokens, output_tokens=0, request_id=request_id,
                          request_sha256=request_sha256, response_sha256=_sha_payload({"embeddings": embeddings}),
                          request_kind='embed')
    return embeddings
