from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

from services.common.llm_gateway import LLMEmbeddingResult


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "services" / "profile-ingestion" / "profile_retrieval_api.py"
SPEC = importlib.util.spec_from_file_location("jobos_profile_retrieval_architecture", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_query_relevance_is_domain_neutral_and_token_exact():
    query_terms = MODULE.tokenize_query("AWS Python")
    assert query_terms == ["aws", "python"]

    exact = {"text_content": "Built Python services on AWS."}
    substring_only = {"text_content": "Creates drawings for security teams."}
    unrelated_cyber = {"text_content": "Linux firewall forensics security."}

    assert MODULE.compute_query_relevance(exact, query_terms) == 0.25
    assert MODULE.compute_query_relevance(substring_only, ["aws"]) == 0.0
    assert MODULE.compute_query_relevance(unrelated_cyber, ["python"]) == 0.0


def test_embed_query_result_preserves_gateway_metadata(monkeypatch):
    fake = LLMEmbeddingResult(
        vectors=[[0.0] * MODULE.EMBED_DIM],
        provider="unit-provider",
        configured_model="configured-model",
        model="resolved-model",
        tokens_input=11,
        estimated_cost_usd=0.25,
        request_id="req-1",
    )
    monkeypatch.setattr(MODULE, "embed_result", lambda **_kwargs: fake)
    result = MODULE.embed_query_result("unit query")
    assert result is fake
    assert MODULE.embed_query("unit query") == [0.0] * MODULE.EMBED_DIM


class _Cursor:
    def __init__(self):
        self.calls = []
        self._ids = iter((("component-1",), ("retrieval-1",)))

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return next(self._ids)


def test_save_retrieval_records_actual_provider_but_filters_by_configured_model():
    cur = _Cursor()
    args = Namespace(
        purpose="resume",
        role_family="software",
        retrieval_intent="evidence",
        query="python",
        skills=[],
        max_chunks=5,
        min_similarity=0.0,
    )
    embedding = LLMEmbeddingResult(
        vectors=[[0.0] * MODULE.EMBED_DIM],
        provider="gemini",
        configured_model="embed-alias",
        model="embed-resolved",
        tokens_input=13,
        estimated_cost_usd=0.5,
        request_id="request-1",
    )
    retrieval_id = MODULE.save_retrieval(
        cur=cur,
        args=args,
        query_text="python",
        query_embedding=embedding.vectors[0],
        results=[],
        filters={},
        embedding_result=embedding,
    )
    assert retrieval_id == "retrieval-1"

    component_params = cur.calls[0][1]
    assert component_params[-4:] == ("gemini", "embed-resolved", 13, 0.5)
    retrieval_params = cur.calls[1][1]
    assert retrieval_params[4] == "embed-alias"
    assert retrieval_params[5] == MODULE.EMBED_DIM
