"""Stable, non-secret bindings for an approved deterministic form session."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Mapping


def canonical_page_url(value: str) -> str:
    """Canonicalize a page without discarding job-identifying query values."""
    parsed = urlsplit((value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Expected page URL must be an absolute HTTP(S) URL.")
    # Query parameters routinely carry an ATS requisition/job identifier.
    # Preserve all of them (but normalize ordering/encoding); stripping only
    # the fragment avoids treating job=123 and job=456 as the same capability.
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                       parsed.path.rstrip("/") or "/", query, ""))


def page_fingerprint(snapshot_payload: Mapping[str, Any], *, page_url: str = "") -> str:
    """Hash stable application anchors, not dynamic form controls.

    A React form may reveal conditional controls after a legitimate write. The
    exact canonical URL remains the primary identity binding; this fingerprint
    adds only stable form/heading anchors and intentionally excludes inputs,
    radios, selects, and their values.
    """
    anchors = []
    for line in str(snapshot_payload.get("snapshot") or "").splitlines():
        stable = line.split("[ref=", 1)[0].strip()
        role = stable.removeprefix("-").strip().split(" ", 1)[0].casefold()
        if role in {"heading", "main", "form", "article", "dialog", "banner"}:
            anchors.append(" ".join(stable.split()))
    # Some ATS snapshots omit semantic container roles. The exact URL still
    # provides a secure identity; an empty structure must not force a false
    # reconciliation after an otherwise valid conditional form update.
    if not anchors and page_url:
        anchors.append(f"url:{canonical_page_url(page_url)}")
    if not anchors:
        raise ValueError("Cannot bind an approval to an empty browser page identity.")
    return hashlib.sha256("\n".join(anchors).encode("utf-8")).hexdigest()


def autofill_input_hash(*, profile: Mapping[str, Any], sensitive_answers: Mapping[str, Any],
                        document_sha256: str, artifact_sha256: str | None,
                        page_url: str, page_fingerprint_sha256: str) -> str:
    # The DB view's named values are the canonical static profile snapshot.
    # Runtime-only document paths must not affect a capability: artifact bytes
    # are bound independently by SHA-256.
    profile_snapshot = profile.get("_approval_ready_values", profile)
    payload = {"profile": profile_snapshot, "sensitive_answers": sensitive_answers,
               "document_sha256": document_sha256, "artifact_sha256": artifact_sha256,
               "page_url": canonical_page_url(page_url), "page_fingerprint_sha256": page_fingerprint_sha256}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
