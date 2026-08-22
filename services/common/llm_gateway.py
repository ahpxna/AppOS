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
import json
import os
import urllib.error
import urllib.request
from typing import Any, Sequence

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


def _role_prefix(role: str) -> str:
    return role.upper().replace("-", "_")


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
            api_key=None, api_style="ollama",
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
    return LLMConfig(role=role, backend=backend, model=api_model,
                     base_url=base_url, api_key=api_key, api_style=api_style)


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
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMGatewayError(f"LLM HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMGatewayError(f"LLM request failed for {url}: {exc}") from exc


def generate_text(*, role: str, prompt: str, model: str | None = None,
                  local_url: str | None = None, timeout: int = 300,
                  temperature: float = 0.2, num_ctx: int | None = None) -> str:
    """Generate one text response through the configured backend.

    Shared response validation prevents migrated services from silently saving
    unusable output when a provider returns an empty response.
    """
    config = resolve_config(role=role, model=model, local_url=local_url)
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
        }
        response = _post_json(_api_endpoint(config.base_url, "chat/completions", style=config.api_style), payload,
                              timeout=timeout, api_key=config.api_key)
        choices = response.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") if choices else None)
    if not isinstance(text, str) or not text.strip():
        raise LLMGatewayError(f"{config.backend} backend returned no text for role={role!r}.")
    return text


def chat_text(*, role: str, messages: Sequence[dict[str, str]], model: str | None = None,
              local_url: str | None = None, timeout: int = 300,
              temperature: float = 0.2, num_ctx: int | None = None,
              json_mode: bool = False,
              json_schema: dict[str, Any] | None = None) -> str:
    """Send a multi-message request through the selected backend.

    ``json_mode`` asks for a JSON object. ``json_schema`` additionally gives
    Ollama its native schema constraint; API providers receive the portable
    JSON-object mode because exact-schema support is not universal.
    """
    config = resolve_config(role=role, model=model, local_url=local_url)
    if config.backend == "ollama":
        options: dict[str, Any] = {"temperature": temperature}
        if num_ctx:
            options["num_ctx"] = num_ctx
        payload: dict[str, Any] = {
            "model": config.model, "messages": list(messages), "stream": False, "options": options,
        }
        if json_schema is not None:
            payload["format"] = json_schema
        elif json_mode:
            payload["format"] = "json"
        response = _post_json(config.base_url + "/api/chat", payload, timeout=timeout)
        text = (response.get("message") or {}).get("content")
    else:
        payload = {
            "model": config.model, "messages": list(messages), "temperature": temperature,
        }
        if json_mode or json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        response = _post_json(_api_endpoint(config.base_url, "chat/completions", style=config.api_style), payload,
                              timeout=timeout, api_key=config.api_key)
        choices = response.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") if choices else None)
    if not isinstance(text, str) or not text.strip():
        raise LLMGatewayError(f"{config.backend} backend returned no chat text for role={role!r}.")
    return text


def embed_texts(*, texts: Sequence[str], model: str | None = None,
                local_url: str | None = None, timeout: int = 180) -> list[list[float]]:
    """Embed a batch through the selected backend, with legacy-Ollama fallback."""
    config = resolve_config(role="embed", model=model, local_url=local_url)
    if not texts:
        return []
    if config.backend == "ollama":
        response = _post_json(config.base_url + "/api/embed", {
            "model": config.model, "input": list(texts),
        }, timeout=timeout)
        embeddings = response.get("embeddings")
        if embeddings is None:  # Supports older Ollama servers one text at a time.
            embeddings = []
            for text in texts:
                legacy = _post_json(config.base_url + "/api/embeddings", {
                    "model": config.model, "prompt": text,
                }, timeout=timeout)
                embeddings.append(legacy.get("embedding"))
    else:
        if config.api_style == "deepseek":
            raise LLMGatewayError(
                "The DeepSeek API style is configured for role='embed'. "
                "Configure JOBOS_EMBED_LLM_API_* to an embedding-capable "
                "OpenAI-compatible provider instead."
            )
        response = _post_json(_api_endpoint(config.base_url, "embeddings", style=config.api_style), {
            "model": config.model, "input": list(texts),
        }, timeout=timeout, api_key=config.api_key)
        embeddings = [item.get("embedding") for item in (response.get("data") or [])]
    if not isinstance(embeddings, list) or len(embeddings) != len(texts) or any(not isinstance(row, list) for row in embeddings):
        raise LLMGatewayError("Embedding backend returned an invalid vector payload.")
    return embeddings
