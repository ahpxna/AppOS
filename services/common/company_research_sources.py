"""Normalize historical company-research source metadata.

``company_research_cache.sources`` has existed in several shapes across JobOS
releases: a flat URL list, legacy ``{"type": ..., "url": ...}`` objects, and
the current metadata envelope. Consumers use this module so a compatible cache
row never loses provenance merely because one release represented the metadata
differently.
"""
from __future__ import annotations

from typing import Any


_EVIDENCE_FIELDS = ("summary", "mission", "products")


def _http_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://")) else ""


def company_research_source_urls(raw: Any) -> set[str]:
    """Return HTTP(S) source URLs from every supported cache representation.

    Traversal is deliberately narrow: URL authority is taken only from a
    top-level URL string, list/tuple/set entries, an object's ``url`` field, or
    the envelope fields ``urls``/``sources``. Arbitrary strings elsewhere in
    metadata are never promoted to source authority.
    """
    found: set[str] = set()

    def add(value: Any) -> None:
        url = _http_url(value)
        if url:
            found.add(url)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        if isinstance(value, dict):
            add(value.get("url"))
            add(value.get("urls"))
            add(value.get("sources"))

    add(raw)
    return found


def company_research_field_evidence(raw: Any) -> dict[str, list[dict[str, str]]]:
    """Return validated per-field evidence from the current metadata envelope.

    A URL in metadata is not by itself evidence for a company claim.  New rows
    additionally bind summary/mission/products to a short source excerpt. This
    helper ignores malformed entries and entries whose URL is not one of the
    cache row's declared source URLs. Legacy rows simply return an empty map and
    can be refreshed without breaking older cache reads.
    """
    if not isinstance(raw, dict):
        return {}
    allowed_urls = company_research_source_urls(raw)
    payload = raw.get("field_evidence")
    if not isinstance(payload, dict):
        return {}

    out: dict[str, list[dict[str, str]]] = {}
    for field in _EVIDENCE_FIELDS:
        entries = payload.get(field)
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        kept: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in entries:
            if not isinstance(item, dict):
                continue
            url = _http_url(item.get("source_url") or item.get("url"))
            quote = " ".join(str(item.get("supporting_quote") or item.get("quote") or "").split())
            if not url or url not in allowed_urls or not quote:
                continue
            key = (url, quote)
            if key in seen:
                continue
            seen.add(key)
            kept.append({"source_url": url, "supporting_quote": quote})
        if kept:
            out[field] = kept
    return out


def company_research_evidence_text(raw: Any) -> str:
    """Flatten only authoritative source excerpts for literal-quote checking."""
    evidence = company_research_field_evidence(raw)
    return "\n".join(
        item["supporting_quote"]
        for field in _EVIDENCE_FIELDS
        for item in evidence.get(field, [])
        if item.get("supporting_quote")
    )
