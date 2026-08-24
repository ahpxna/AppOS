"""Pinned-tab OpenClaw transport; no prompts and no arbitrary local uploads."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class AutofillTransport(Protocol):
    def resolve_target(self) -> "BrowserTarget": ...
    def current_url(self, target_id: str) -> str: ...
    def snapshot(self, target_id: str) -> dict[str, Any]: ...
    def execute(self, target_id: str, command: dict[str, str]) -> None: ...


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserTarget:
    target_id: str
    url: str


class OpenClawTransport:
    """Use only documented OpenClaw browser primitives on one stable tab."""
    def __init__(self, *, binary: str = "openclaw", profile: str = "remote", timeout: int = 60,
                 environment: dict[str, str] | None = None, uploads_dir: Path | None = None,
                 approved_upload_hashes: dict[str, str] | None = None):
        self.binary, self.profile, self.timeout = binary, profile, timeout
        self.environment = environment or dict(os.environ)
        self.uploads_dir = uploads_dir or Path(os.getenv("JOBOS_OPENCLAW_UPLOADS_DIR", "/tmp/openclaw/uploads"))
        # ``None`` means this transport is read-only/non-uploading.  Production
        # autofill passes an explicit map (possibly empty), so an upload without
        # an exact approval-bound SHA-256 fails closed.
        self.approved_upload_hashes = None if approved_upload_hashes is None else {
            str(Path(path).expanduser().resolve()): str(digest).casefold()
            for path, digest in approved_upload_hashes.items()
        }

    def _run(self, args: list[str], *, json_output: bool = False) -> str:
        if shutil.which(self.binary) is None:
            raise TransportError(f"OpenClaw binary not found: {self.binary}")
        # Keep ``--browser-profile`` adjacent to its value.  OpenClaw's CLI
        # examples put ``--json`` after the browser subcommand, which also
        # works across the pinned CLI versions.
        command = [self.binary, "browser", "--browser-profile", self.profile, *args]
        if json_output:
            command.append("--json")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, env=self.environment)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"OpenClaw browser command timed out: {args[0]}") from exc
        if result.returncode != 0:
            raise TransportError((result.stderr or result.stdout or "OpenClaw browser failure").strip()[:500])
        return result.stdout

    @staticmethod
    def _json(output: str) -> Any:
        starts = [index for index in (output.find("{"), output.find("[")) if index >= 0]
        if not starts:
            raise TransportError("OpenClaw browser command did not return JSON.")
        first, last = min(starts), max(output.rfind("}"), output.rfind("]"))
        try:
            return json.loads(output[first:last + 1])
        except (json.JSONDecodeError, ValueError) as exc:
            raise TransportError("OpenClaw browser returned malformed JSON.") from exc

    def _tabs(self) -> list[dict[str, Any]]:
        data = self._json(self._run(["tabs"], json_output=True))
        nested = data.get("data") if isinstance(data, dict) else None
        tabs = data if isinstance(data, list) else (data.get("tabs") or data.get("pages") or
               (nested.get("tabs") if isinstance(nested, dict) else []) or [])
        if not isinstance(tabs, list):
            raise TransportError("OpenClaw did not provide a tab list.")
        return [item for item in tabs if isinstance(item, dict)]

    @staticmethod
    def _stable_id(tab: dict[str, Any]) -> str:
        return str(tab.get("suggestedTargetId") or tab.get("tabId") or "")

    def resolve_target(self) -> BrowserTarget:
        tabs = self._tabs()
        active = [tab for tab in tabs if any(tab.get(key) is True for key in ("active", "focused", "selected"))]
        candidates = active or (tabs if len(tabs) == 1 else [])
        if len(candidates) != 1:
            raise TransportError("Cannot identify exactly one focused browser tab to pin.")
        tab = candidates[0]
        target_id, url = self._stable_id(tab), str(tab.get("url") or "")
        if not target_id or not url.startswith(("https://", "http://")):
            raise TransportError("Focused OpenClaw tab has no stable target id or HTTP(S) URL.")
        return BrowserTarget(target_id, url)

    def current_url(self, target_id: str) -> str:
        for tab in self._tabs():
            if target_id in {self._stable_id(tab), str(tab.get("targetId") or "")}:
                url = str(tab.get("url") or "")
                if url.startswith(("https://", "http://")):
                    return url
        raise TransportError("Pinned browser tab disappeared or cannot be resolved.")

    def snapshot(self, target_id: str) -> dict[str, Any]:
        data = self._json(self._run(["snapshot", "--efficient", "--target-id", target_id], json_output=True))
        if not isinstance(data, dict):
            raise TransportError("OpenClaw snapshot JSON is not an object.")
        return data

    @staticmethod
    def _find_media_path(value: Any) -> Path | None:
        """Find an existing local screenshot path in OpenClaw output."""
        if isinstance(value, dict):
            for key in ("path", "file", "mediaPath", "screenshotPath", "outputPath"):
                raw = value.get(key)
                if isinstance(raw, str):
                    candidate = Path(raw.removeprefix("file://")).expanduser()
                    if candidate.is_file():
                        return candidate.resolve()
            for nested in value.values():
                found = OpenClawTransport._find_media_path(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = OpenClawTransport._find_media_path(nested)
                if found:
                    return found
        elif isinstance(value, str):
            for token in value.replace("\n", " ").split():
                raw = token.strip("\"'(),[]{}")
                if raw.startswith("file://"):
                    raw = raw[7:]
                candidate = Path(raw).expanduser()
                if candidate.is_file() and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    return candidate.resolve()
        return None

    def screenshot(self, target_id: str, *, full_page: bool = True) -> Path:
        """Capture only the already-pinned tab; never invoke an LLM agent."""
        args = ["screenshot", "--target-id", target_id]
        if full_page:
            args.append("--full-page")
        try:
            raw = self._run(args, json_output=True)
            try:
                parsed = self._json(raw)
            except TransportError:
                parsed = raw
            path = self._find_media_path(parsed)
            if path:
                return path
        except TransportError:
            if not full_page:
                raise
            raw = self._run(["screenshot", "--target-id", target_id], json_output=True)
            try:
                parsed = self._json(raw)
            except TransportError:
                parsed = raw
            path = self._find_media_path(parsed)
            if path:
                return path
        raise TransportError("OpenClaw screenshot completed without an accessible local image path.")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _stage_upload(self, value: str) -> str:
        source = Path(value).expanduser().resolve()
        if not source.is_file():
            raise TransportError(f"Approved upload artifact is missing: {source}")
        digest = self._sha256_file(source)
        if self.approved_upload_hashes is not None:
            expected = self.approved_upload_hashes.get(str(source))
            if not expected:
                raise TransportError(
                    "Upload path is not bound to an approval SHA-256; refusing unapproved bytes."
                )
            if digest.casefold() != expected:
                raise TransportError(
                    "Approved upload artifact bytes changed after preflight; issue a fresh approval."
                )
        else:
            expected = digest

        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        staged = self.uploads_dir / f"{expected[:16]}-{source.name}"
        if not staged.exists() or self._sha256_file(staged) != expected:
            shutil.copyfile(source, staged)
            staged.chmod(0o600)
        # Re-hash the actual staged bytes.  This closes the copy-time race where
        # the source changes after the first digest but before/during copy.
        if self._sha256_file(staged) != expected:
            try:
                staged.unlink()
            except OSError:
                pass
            raise TransportError(
                "Staged upload bytes do not match the approval-bound SHA-256; refusing upload."
            )
        return str(staged)

    def execute(self, target_id: str, command: dict[str, str]) -> None:
        action, ref, value = command.get("action"), command.get("target"), command.get("value")
        if not action or not ref or value is None:
            raise TransportError("Malformed deterministic browser command.")
        scope = ["--target-id", target_id]
        if action == "fill":
            payload = json.dumps([{"ref": ref, "value": value}], separators=(",", ":"))
            self._run(["fill", "--fields", payload, *scope])
        elif action == "select":
            self._run(["select", ref, value, *scope])
        elif action == "check":
            self._run(["click", ref, *scope])
        elif action == "upload":
            self._run(["upload", self._stage_upload(value), "--ref", ref, *scope])
        else:
            raise TransportError(f"Unsupported deterministic browser action: {action}")
