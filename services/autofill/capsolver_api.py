import os
import time
import requests
import logging

from services.common.config import env_float, load_repo_env

log = logging.getLogger(__name__)


def _config() -> tuple[str, float, float, float]:
    """Read CapSolver config at call time so repo .env loading order cannot stale-cache it."""
    load_repo_env()
    key = (os.getenv("CAPSOLVER_API_KEY") or "").strip()
    if not key or key == "your_capsolver_key_here":
        raise ValueError("Missing CAPSOLVER_API_KEY in .env")
    request_timeout = env_float("CAPSOLVER_REQUEST_TIMEOUT_SECONDS", 10.0, minimum=2.0, maximum=30.0)
    solve_timeout = env_float("CAPSOLVER_SOLVE_TIMEOUT_SECONDS", 120.0, minimum=15.0, maximum=300.0)
    poll_interval = env_float("CAPSOLVER_POLL_INTERVAL_SECONDS", 3.0, minimum=1.0, maximum=10.0)
    return key, request_timeout, solve_timeout, poll_interval


def _post_json(url: str, payload: dict, *, timeout: float) -> dict:
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"CapSolver request failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("CapSolver returned non-JSON data") from exc
    if not isinstance(data, dict):
        raise RuntimeError("CapSolver returned an invalid response")
    return data


def solve_captcha(website_url: str, website_key: str, captcha_type: str = "ReCaptchaV2TaskProxyLess") -> str:
    key, request_timeout, solve_timeout, poll_interval = _config()
    payload = {
        "clientKey": key,
        "task": {
            "type": captcha_type,
            "websiteURL": website_url,
            "websiteKey": website_key,
        },
    }

    log.info("[CapSolver] Creating bounded task for %s", website_url)
    res = _post_json("https://api.capsolver.com/createTask", payload, timeout=request_timeout)
    if res.get("errorId", 0) > 0:
        raise RuntimeError(f"CapSolver Error: {res.get('errorDescription') or res.get('errorCode') or 'unknown'}")

    task_id = res.get("taskId")
    if not task_id:
        raise RuntimeError("CapSolver did not return a taskId")
    deadline = time.monotonic() + solve_timeout
    log.info("[CapSolver] Task ID: %s. Waiting up to %.0fs", task_id, solve_timeout)

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        res = _post_json(
            "https://api.capsolver.com/getTaskResult",
            {"clientKey": key, "taskId": task_id},
            timeout=request_timeout,
        )
        if res.get("errorId", 0) > 0:
            raise RuntimeError(f"CapSolver Error: {res.get('errorDescription') or res.get('errorCode') or 'unknown'}")
        status = res.get("status")
        if status == "ready":
            solution = res.get("solution") or {}
            token = solution.get("gRecaptchaResponse") or solution.get("token") or solution.get("text")
            if not token:
                raise RuntimeError("CapSolver reported ready without a usable solution token")
            return str(token)
        if status == "failed":
            raise RuntimeError("CapSolver reported task failure")
        if status not in {"processing", "idle", None}:
            raise RuntimeError(f"CapSolver returned unexpected status: {status!r}")
    raise TimeoutError(f"CapSolver did not finish within {solve_timeout:.0f}s")
