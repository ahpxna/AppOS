"""Deterministic read-only browser fallback for JavaScript-only ATS boards.

This module is deliberately *not* an autofill/browser-write adapter.  It may
open public HTTP(S) pages and take accessibility snapshots; it never clicks,
fills, selects, uploads, authenticates, solves checkpoints, or submits.  The
result still has to pass the same complete-JD quality gate as API/JSON-LD
discovery before it can enter the canonical intake pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Protocol
from urllib.parse import urljoin

from services.ats.contracts import JDQuality, WorkMode, assess_jd_quality, canonical_job_url, infer_work_mode
from services.ats.public_page import is_candidate_job_link
from services.ats.registry import detect_ats_platform


class BrowserDiscoveryError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "browser_discovery", transient: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.transient = bool(transient)


class BrowserDiscoveryTransport(Protocol):
    def open(self, url: str): ...
    def snapshot(self, target_id: str) -> dict[str, Any]: ...
    def current_url(self, target_id: str) -> str: ...
    def focus(self, target_id: str): ...
    def close(self, target_id: str) -> None: ...


_URL_RE = re.compile(r"https?://[^\s\]\[<>\"']+")
_HEADING_RE = re.compile(r"^\s*-\s+heading(?:\s+\"(?P<name>[^\"]+)\")?", re.I)
_TREE_PREFIX_RE = re.compile(r"^\s*-\s+[A-Za-z][\w-]*(?:\s+\"([^\"]*)\")?(?:\s*(?:\[[^\]]*\])*)?(?::\s*(.*))?$")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _walk_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_strings(nested)


def _snapshot_urls(payload: dict[str, Any], *, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for text in _walk_strings(payload):
        candidates = list(_URL_RE.findall(text))
        # OpenClaw snapshots may expose relative `/url:` metadata.
        stripped = text.strip()
        if stripped.startswith("/url:"):
            candidates.append(stripped.split(":", 1)[1].strip())
        for raw in candidates:
            try:
                absolute = canonical_job_url(urljoin(base_url, raw.rstrip(".,);")))
            except ValueError:
                continue
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)
    return urls


def _visible_snapshot_text(payload: dict[str, Any]) -> str:
    raw = str(payload.get("snapshot") or "")
    parts: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("/url:", "/placeholder:", "/value:")):
            continue
        match = _TREE_PREFIX_RE.match(line)
        if match:
            label, value = match.groups()
            for candidate in (label, value):
                candidate = str(candidate or "").strip()
                if candidate and candidate != "*" and not candidate.startswith("/"):
                    parts.append(candidate)
            continue
        # Keep meaningful refless text but drop tree/reference syntax.
        cleaned = re.sub(r"\[ref=[^\]]+\]", "", stripped).strip(" -")
        if cleaned and not cleaned.startswith("/"):
            parts.append(cleaned)
    # De-duplicate adjacent/repeated accessibility labels without reordering.
    out: list[str] = []
    seen_recent: set[str] = set()
    for part in parts:
        key = re.sub(r"\s+", " ", part).strip()
        if not key or key in seen_recent:
            continue
        out.append(key)
        seen_recent.add(key)
        if len(seen_recent) > 80:
            seen_recent = set(out[-40:])
    return "\n".join(out).strip()


def _snapshot_title(payload: dict[str, Any]) -> str:
    refs = payload.get("refs") or {}
    if isinstance(refs, dict):
        for meta in refs.values():
            if isinstance(meta, dict) and str(meta.get("role") or "").casefold() == "heading":
                name = " ".join(str(meta.get("name") or "").split())
                if name:
                    return name[:300]
    for line in str(payload.get("snapshot") or "").splitlines():
        match = _HEADING_RE.match(line)
        if match and match.group("name"):
            return " ".join(match.group("name").split())[:300]
    return ""


def _normalized_snapshot_job(payload: dict[str, Any], *, page_url: str,
                             company_hint: str) -> dict[str, Any] | None:
    if bool(payload.get("truncated")):
        return None
    text = _visible_snapshot_text(payload)
    if assess_jd_quality(text) != JDQuality.COMPLETE:
        return None
    title = _snapshot_title(payload)
    if not title:
        return None
    try:
        url = canonical_job_url(page_url)
    except ValueError:
        return None
    work_mode = infer_work_mode(text)
    jd_text = f"{title}\nCompany: {company_hint}\n\n{text}".strip()
    if assess_jd_quality(jd_text) != JDQuality.COMPLETE:
        return None
    return {
        "external_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
        "title": title,
        "location": "",
        "department": "",
        "remote": work_mode == WorkMode.REMOTE,
        "work_mode": work_mode.value,
        "url": url,
        "jd_text": jd_text,
        "jd_quality": JDQuality.COMPLETE.value,
        "discovery_method": "readonly_browser_snapshot",
    }


def discover_public_jobs_with_browser(*, career_url: str, platform: str, company_hint: str,
                                      max_details: int = 20,
                                      transport: BrowserDiscoveryTransport | None = None) -> list[dict[str, Any]]:
    """Discover complete jobs from rendered accessibility snapshots only."""
    try:
        board_url = canonical_job_url(career_url)
    except ValueError as exc:
        raise BrowserDiscoveryError(str(exc), kind="invalid_url") from exc

    if transport is None:
        try:
            from services.autofill.autofill_executor_v1 import OpenClawTransport
            from services.common.openclaw_runtime import resolve_openclaw_binary
            transport = OpenClawTransport(
                binary=resolve_openclaw_binary(required=True), profile="remote", timeout=90
            )
        except Exception as exc:
            raise BrowserDiscoveryError(
                f"managed read-only browser discovery unavailable: {exc}", kind="browser_unavailable"
            ) from exc

    board_target_id = ""
    opened_target_ids: list[str] = []
    try:
        board_target = transport.open(board_url)
        board_target_id = str(board_target.target_id)
        opened_target_ids.append(board_target_id)
        rendered_board_url = transport.current_url(board_target_id)
        board_snapshot = transport.snapshot(board_target_id)
        if bool(board_snapshot.get("truncated")):
            raise BrowserDiscoveryError(
                "rendered ATS board snapshot is truncated; refusing incomplete discovery",
                kind="truncated_snapshot",
            )

        detail_urls: list[str] = []
        seen_urls: set[str] = set()
        for url in _snapshot_urls(board_snapshot, base_url=rendered_board_url):
            if url in seen_urls:
                continue
            if is_candidate_job_link(url, board_url=rendered_board_url, platform=platform):
                # A custom company page may hand off only to a registry-known ATS.
                if (url.split("/", 3)[2].casefold() != rendered_board_url.split("/", 3)[2].casefold()
                        and platform == "custom" and detect_ats_platform(url) == "custom"):
                    continue
                seen_urls.add(url)
                detail_urls.append(url)

        # If the supplied URL is already an exact job detail, parse it directly.
        jobs: list[dict[str, Any]] = []
        if is_candidate_job_link(rendered_board_url, board_url=rendered_board_url, platform=platform):
            item = _normalized_snapshot_job(
                board_snapshot, page_url=rendered_board_url, company_hint=company_hint
            )
            if item:
                jobs.append(item)

        for detail_url in detail_urls[:max(1, min(int(max_details), 50))]:
            detail_target = transport.open(detail_url)
            detail_target_id = str(detail_target.target_id)
            opened_target_ids.append(detail_target_id)
            final_url = transport.current_url(detail_target_id)
            snapshot = transport.snapshot(detail_target_id)
            item = _normalized_snapshot_job(snapshot, page_url=final_url, company_hint=company_hint)
            if item and all(existing["url"] != item["url"] for existing in jobs):
                jobs.append(item)
        if not jobs:
            raise BrowserDiscoveryError(
                f"{platform} rendered board exposed no complete deterministic job-detail snapshots",
                kind="incomplete_or_missing_jobposting",
            )
        return jobs
    except BrowserDiscoveryError:
        raise
    except Exception as exc:
        detail = str(exc)
        transient = "timed out" in detail.casefold() or "timeout" in detail.casefold()
        raise BrowserDiscoveryError(detail or "read-only browser discovery failed", transient=transient) from exc
    finally:
        # Read-only discovery owns every tab it opens. Close them in reverse order
        # so periodic polling cannot leak browser targets. `browser close <id>` is
        # a documented OpenClaw primitive; failures remain cleanup-only and never
        # overwrite the discovery result/error that caused this finally block.
        for target_id in reversed(opened_target_ids):
            try:
                transport.close(target_id)
            except Exception:
                pass
