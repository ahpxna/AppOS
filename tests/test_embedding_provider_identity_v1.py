from __future__ import annotations

import importlib.util
from pathlib import Path

from services.common.llm_gateway import LLMEmbeddingResult


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD = _load("jobos_embed_profile_chunks_arch", "services/profile-ingestion/embed_profile_chunks.py")
V2 = _load("jobos_embed_profile_chunks_v2_arch", "services/profile-ingestion/embed_profile_chunks_v2.py")


def _result(dim: int) -> LLMEmbeddingResult:
    return LLMEmbeddingResult(
        vectors=[[0.25] * dim],
        provider="unit-provider",
        configured_model="configured-embed",
        model="resolved-embed",
        tokens_input=17,
        estimated_cost_usd=0.125,
        request_id="request-1",
    )


def test_legacy_embedder_preserves_gateway_embedding_identity(monkeypatch):
    fake = _result(OLD.EMBED_DIM)
    monkeypatch.setattr(OLD, "embed_result", lambda **_kwargs: fake)
    assert OLD.embed_text_result("evidence") is fake
    assert OLD.embed_text("evidence") == fake.vectors[0]


def test_v2_embedder_preserves_gateway_embedding_identity(monkeypatch):
    fake = _result(768)
    monkeypatch.setattr(V2, "embed_result", lambda **_kwargs: fake)
    assert V2.embed_text_result("evidence", retries=1) is fake
    assert V2.embed_text("evidence", retries=1) == fake.vectors[0]


class _Cursor:
    def __init__(self):
        self.params = None

    def execute(self, _sql, params):
        self.params = params

    def fetchall(self):
        return []


def test_canonical_embedder_scans_eligible_chunks_before_exact_content_identity_filter():
    cur = _Cursor()
    assert OLD.fetch_chunks(cur, 12, "configured-embed") == []
    # Exact currentness includes provider + configured model + content hash and
    # is evaluated after canonical embedding text is hashed. Pre-filtering by
    # model alone would strand stale vectors when chunk content changes.
    assert cur.params == ()
