"""Bounded HTTP contract for public ATS discovery adapters."""
from __future__ import annotations

from dataclasses import dataclass
import json
import random
import time
import urllib.error
import urllib.request
from typing import Any


@dataclass(frozen=True)
class DiscoveryHttpError(RuntimeError):
    kind: str
    url: str
    transient: bool
    status: int | None = None
    retry_after_seconds: float | None = None
    body_preview: str = ""

    def __str__(self) -> str:
        suffix = f" HTTP {self.status}" if self.status is not None else ""
        return f"{self.kind}{suffix} fetching {self.url}"


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers else None
    try:
        return max(0.0, min(float(str(raw)), 120.0))
    except (TypeError, ValueError):
        return None


def get_json(*, url: str, user_agent: str, timeout_seconds: int = 30,
             attempts: int = 3, sleep=time.sleep, jitter=None) -> Any:
    """Fetch one public JSON endpoint with bounded retry and typed failure.

    Only availability/rate-limit failures are retried.  A malformed payload
    or a permanent 4xx is an adapter/data failure, never a reason to spin the
    discovery worker or disable a company permanently.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    last: DiscoveryHttpError | None = None
    for attempt in range(max(1, min(int(attempts), 5))):
        try:
            with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
                body = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise DiscoveryHttpError("invalid_json", url, False, body_preview=body[:1000]) from exc
        except DiscoveryHttpError:
            raise
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
            last = DiscoveryHttpError(
                "http", url, retryable, status=exc.code, retry_after_seconds=_retry_after(exc.headers),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = DiscoveryHttpError("network", url, True)
        if not last.transient or attempt + 1 >= max(1, min(int(attempts), 5)):
            raise last
        base_delay = last.retry_after_seconds if last.retry_after_seconds is not None else min(2 ** attempt, 4)
        delay = jitter(base_delay) if jitter is not None else base_delay * random.uniform(0.8, 1.2)
        sleep(max(0.0, delay))
    raise last or DiscoveryHttpError("network", url, True)


def get_text(*, url: str, user_agent: str, timeout_seconds: int = 30,
             attempts: int = 3, sleep=time.sleep, jitter=None,
             accept: str = "text/html,application/xhtml+xml") -> tuple[str, str]:
    """Fetch public HTML/text with bounded retry and return ``(body, final_url)``.

    The final URL matters because many career pages redirect to an ATS tenant;
    downstream identity/detection must bind to the resolved target rather than
    a presentation redirect on the employer domain.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": accept})
    last: DiscoveryHttpError | None = None
    for attempt in range(max(1, min(int(attempts), 5))):
        try:
            with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
                body = response.read().decode("utf-8", errors="replace")
                final_url = response.geturl() or url
            return body, final_url
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
            last = DiscoveryHttpError(
                "http", url, retryable, status=exc.code, retry_after_seconds=_retry_after(exc.headers),
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            last = DiscoveryHttpError("network", url, True)
        if not last.transient or attempt + 1 >= max(1, min(int(attempts), 5)):
            raise last
        base_delay = last.retry_after_seconds if last.retry_after_seconds is not None else min(2 ** attempt, 4)
        delay = jitter(base_delay) if jitter is not None else base_delay * random.uniform(0.8, 1.2)
        sleep(max(0.0, delay))
    raise last or DiscoveryHttpError("network", url, True)
