from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional


def make_trace_id(*parts: str) -> str:
    clean_parts = [part for part in parts if part]
    if clean_parts:
        digest = hashlib.sha256("::".join(clean_parts).encode("utf-8")).hexdigest()
        return digest[:16]
    return secrets.token_hex(8)


def emit_trace(
    trace_id: str,
    stage: str,
    *,
    started_at: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: Optional[float] = 0.0,
    status: str = "ok",
    **fields: Any,
) -> None:
    payload = {
        "trace_id": trace_id,
        "stage": stage,
        "duration_ms": round((time.perf_counter() - started_at) * 1000),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "status": status,
    }
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@contextmanager
def traced_stage(
    trace_id: str,
    stage: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: Optional[float] = 0.0,
    status: str = "ok",
    **fields: Any,
) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        emit_trace(
            trace_id,
            stage,
            started_at=start,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status=status,
            **fields,
        )
