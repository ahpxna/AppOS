from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_RUNTIME_IMPORTS = {
    "langchain",
    "langgraph",
    "llama_index",
    "haystack",
    "pinecone",
    "qdrant_client",
    "weaviate",
    "pymilvus",
    "chromadb",
}
AI_PROVIDER_HOSTS = {
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "api.groq.com",
    "api.together.xyz",
    "api.anthropic.com",
    "openrouter.ai",
}


def _production_python_files():
    for base in (ROOT / "services", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if "legacy" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def _import_root(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module.split(".", 1)[0]]
    return []


def test_runtime_does_not_add_a_second_ai_or_vector_control_plane():
    offenders: list[str] = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for root in _import_root(node):
                if root in FORBIDDEN_RUNTIME_IMPORTS:
                    offenders.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}:{root}")
    assert offenders == [], f"AI orchestration/vector authority must remain inside AppOS/PostgreSQL: {offenders}"


def test_production_subprocess_calls_do_not_enable_shell_true():
    offenders: list[str] = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], f"production subprocess shell=True is forbidden: {offenders}"


def test_llm_provider_http_endpoints_remain_centralized_in_gateway():
    gateway = ROOT / "services" / "common" / "llm_gateway.py"
    offenders: list[str] = []
    for path in _production_python_files():
        if path == gateway:
            continue
        text = path.read_text(encoding="utf-8").casefold()
        for host in AI_PROVIDER_HOSTS:
            if host in text:
                offenders.append(f"{path.relative_to(ROOT)}:{host}")
    assert offenders == [], f"direct provider HTTP endpoints bypass llm_gateway.py: {offenders}"


def test_literal_sql_execute_placeholder_counts_match_parameter_tuples():
    offenders: list[str] = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "execute" or len(node.args) < 2:
                continue
            sql, params = node.args[0], node.args[1]
            if not isinstance(sql, ast.Constant) or not isinstance(sql.value, str):
                continue
            if not isinstance(params, (ast.Tuple, ast.List)):
                continue
            expected = sql.value.count("%s")
            actual = len(params.elts)
            if expected != actual:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:placeholders={expected}:params={actual}"
                )
    assert offenders == [], f"literal SQL placeholder/parameter mismatch: {offenders}"
