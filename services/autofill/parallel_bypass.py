import threading
import time
import json
import random
import requests
import websocket
import logging
from urllib.parse import urlsplit

from services.autofill.capsolver_api import solve_captcha

log = logging.getLogger(__name__)


def _is_linkedin_url(url: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _cdp_catalog_url(ws_url: str) -> str:
    parsed = urlsplit(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        raise RuntimeError(f"CDP websocket URL has no port: {ws_url!r}")
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{host}:{port}/json"


def _cdp_tabs(ws_url: str) -> list[dict]:
    try:
        response = requests.get(_cdp_catalog_url(ws_url), timeout=2)
        response.raise_for_status()
        tabs = response.json()
    except Exception as exc:
        raise RuntimeError(f"cannot read CDP target list: {exc}") from exc
    if not isinstance(tabs, list):
        raise RuntimeError("CDP /json did not return a page list")
    return [tab for tab in tabs if isinstance(tab, dict)]


def _seed_target(tabs: list[dict], ws_url: str) -> dict | None:
    for tab in tabs:
        if str(tab.get("webSocketDebuggerUrl") or "") == ws_url:
            return tab
    return None


def _linkedin_page_targets(tabs: list[dict]) -> list[tuple[str, str]]:
    """Return every live LinkedIn page target, deterministically de-duplicated."""
    seen: set[str] = set()
    targets: list[tuple[str, str]] = []
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        page_url = str(tab.get("url") or "")
        target_ws = str(tab.get("webSocketDebuggerUrl") or "")
        if not target_ws or target_ws in seen or not _is_linkedin_url(page_url):
            continue
        seen.add(target_ws)
        targets.append((target_ws, page_url))
    return targets


def _page_url(ws) -> str:
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "location.href", "returnByValue": True}}))
    response = json.loads(ws.recv())
    return str(response.get("result", {}).get("result", {}).get("value") or "")


def _load_regimes(regimes_file_path: str) -> list[dict]:
    with open(regimes_file_path, "r", encoding="utf-8") as f:
        regimes = json.load(f).get("regimes", [])
    return regimes if isinstance(regimes, list) else []


def _fake_mouse_target_routine(
    ws_url: str,
    regimes_file_path: str,
    stop_event: threading.Event,
    *,
    require_linkedin: bool,
) -> None:
    """Run bounded mouse movement on exactly one already-selected CDP page."""
    ws = None
    try:
        ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=3)
        ws.settimeout(3)
        current_url = _page_url(ws)
        if require_linkedin and not _is_linkedin_url(current_url):
            log.warning("[FakeMouse] LinkedIn target changed; refusing stale target: %s", current_url)
            return

        ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": "({width: window.innerWidth, height: window.innerHeight})", "returnByValue": True},
        }))
        size_res = json.loads(ws.recv())
        viewport = size_res.get("result", {}).get("result", {}).get("value", {"width": 1024, "height": 768})
        width = max(1, int(viewport.get("width") or 1024))
        height = max(1, int(viewport.get("height") or 768))
        current_x = random.uniform(min(100, width), max(min(100, width), width - 1))
        current_y = random.uniform(min(100, height), max(min(100, height), height - 1))

        regimes = _load_regimes(regimes_file_path)
        if not regimes:
            log.warning("[FakeMouse] No pointer regimes available; exiting safely")
            return

        dt = 0.05
        while not stop_event.is_set():
            for regime in regimes:
                if stop_event.is_set():
                    break
                drift_x = float((regime.get("drift") or {}).get("x") or 0)
                drift_y = float((regime.get("drift") or {}).get("y") or 0)
                for _step in range(20):
                    if stop_event.wait(dt):
                        break
                    current_x = max(0, min(current_x + (drift_x * dt) + random.gauss(0, 2.0), width))
                    current_y = max(0, min(current_y + (drift_y * dt) + random.gauss(0, 2.0), height))
                    ws.send(json.dumps({
                        "id": random.randint(1000, 9999),
                        "method": "Input.dispatchMouseEvent",
                        "params": {"type": "mouseMoved", "x": int(current_x), "y": int(current_y)},
                    }))
    except Exception as exc:
        log.error("[FakeMouse] bounded target helper stopped for %s: %s", ws_url, exc)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _fake_mouse_linkedin_controller(ws_url: str, regimes_file_path: str, stop_event: threading.Event) -> None:
    """Cover all live LinkedIn page tabs, including tabs opened after the task starts.

    The frozen discovery handlers still hand us one seed websocket.  The seed is
    used only to locate the CDP catalog; every LinkedIn *page* target is then
    independently validated by the child worker before any mouse event is sent.
    Non-LinkedIn tabs are never enrolled by this controller.
    """
    workers: dict[str, threading.Thread] = {}
    retry_after: dict[str, float] = {}
    refresh_seconds = 1.0
    try:
        while not stop_event.is_set():
            try:
                tabs = _cdp_tabs(ws_url)
                live_targets = dict(_linkedin_page_targets(tabs))
            except Exception as exc:
                log.warning("[FakeMouse] cannot refresh LinkedIn CDP targets: %s", exc)
                if stop_event.wait(refresh_seconds):
                    break
                continue

            now = time.monotonic()
            for target_ws, page_url in live_targets.items():
                existing = workers.get(target_ws)
                if existing is not None and existing.is_alive():
                    continue
                if existing is not None:
                    workers.pop(target_ws, None)
                    retry_after[target_ws] = now + 2.0
                if now < retry_after.get(target_ws, 0.0):
                    continue
                thread = threading.Thread(
                    target=_fake_mouse_target_routine,
                    args=(target_ws, regimes_file_path, stop_event),
                    kwargs={"require_linkedin": True},
                    name=f"jobos-fake-mouse-linkedin-{target_ws.rsplit('/', 1)[-1]}",
                    daemon=True,
                )
                workers[target_ws] = thread
                thread.start()
                log.info("[FakeMouse] covering LinkedIn tab %s", page_url)

            # Forget closed targets after their child exits.  The child itself
            # owns/cleans its websocket, so no cross-thread socket close is needed.
            for target_ws, thread in list(workers.items()):
                if target_ws not in live_targets and not thread.is_alive():
                    workers.pop(target_ws, None)
                    retry_after.pop(target_ws, None)

            stop_event.wait(refresh_seconds)
    finally:
        # Child I/O is bounded by websocket timeout and checks the shared stop
        # event.  Bounded joins keep this controller from extending worker exit.
        for thread in list(workers.values()):
            if thread.is_alive():
                thread.join(timeout=1.0)


def _fake_mouse_routine(ws_url: str, regimes_file_path: str, stop_event: threading.Event):
    """Preserve exact ATS autofill mouse behavior and fan out LinkedIn discovery.

    * LinkedIn seed target: cover every live LinkedIn page tab through one outer
      controller.  This removes the old "first/unique page" limitation while
      keeping non-LinkedIn tabs out of the discovery helper.
    * Non-LinkedIn seed target: stay on exactly that websocket.  This preserves
      the existing FakeMouse behavior for employer-site autofill instead of
      accidentally disabling it as the earlier LinkedIn-only guard did.
    """
    try:
        tabs = _cdp_tabs(ws_url)
        seed = _seed_target(tabs, ws_url)
    except Exception as exc:
        log.warning("[FakeMouse] cannot classify seed target; using exact seed only: %s", exc)
        seed = None

    if seed is not None and _is_linkedin_url(str(seed.get("url") or "")):
        _fake_mouse_linkedin_controller(ws_url, regimes_file_path, stop_event)
        return

    _fake_mouse_target_routine(
        ws_url,
        regimes_file_path,
        stop_event,
        require_linkedin=False,
    )


def _select_exact_page(tabs: list[dict], website_url: str) -> str:
    wanted = urlsplit(website_url)
    wanted_host = (wanted.hostname or "").casefold()
    wanted_path = (wanted.path or "/").rstrip("/") or "/"
    candidates = []
    for tab in tabs:
        if tab.get("type") != "page" or not tab.get("webSocketDebuggerUrl"):
            continue
        current = urlsplit(str(tab.get("url") or ""))
        host = (current.hostname or "").casefold()
        path = (current.path or "/").rstrip("/") or "/"
        if host == wanted_host and path == wanted_path:
            candidates.append(tab)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one CDP page for {website_url!r}; found {len(candidates)}")
    return str(candidates[0]["webSocketDebuggerUrl"])


def _inject_solution(ws_url: str, solution_token: str, captcha_type: str) -> None:
    token_js = json.dumps(str(solution_token))
    ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=3)
    try:
        ws.settimeout(3)
        if captcha_type.casefold().startswith("funcaptcha"):
            expression = (
                "(() => { const token = " + token_js + "; "
                "const el = document.querySelector('input[name=\"fc-token\"],#fc-token'); "
                "if (!el) return {ok:false,reason:'fc-token-not-found'}; "
                "el.value = token; el.setAttribute('value', token); "
                "el.dispatchEvent(new Event('input',{bubbles:true})); "
                "el.dispatchEvent(new Event('change',{bubbles:true})); "
                "if (typeof captchaCallback === 'function') captchaCallback(token); "
                "return {ok:true}; })()"
            )
        else:
            expression = (
                "(() => { const token = " + token_js + "; "
                "const el = document.getElementById('g-recaptcha-response'); "
                "if (!el) return {ok:false,reason:'g-recaptcha-response-not-found'}; "
                "el.value = token; el.innerHTML = token; "
                "el.dispatchEvent(new Event('input',{bubbles:true})); "
                "el.dispatchEvent(new Event('change',{bubbles:true})); "
                "if (typeof captchaCallback === 'function') captchaCallback(token); "
                "return {ok:true}; })()"
            )
        ws.send(json.dumps({"id": 9999, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}))
        response = json.loads(ws.recv())
        value = response.get("result", {}).get("result", {}).get("value") or {}
        if not value.get("ok"):
            raise RuntimeError(f"captcha token injection could not bind to the expected DOM control: {value}")
    finally:
        ws.close()


def execute_parallel_bypass(cdp_port: int, website_url: str, website_key: str, regimes_path: str,
                            captcha_type: str = "ReCaptchaV2TaskProxyLess"):
    """Bounded orchestrator preserving the existing CapSolver/FakeMouse feature."""
    try:
        response = requests.get(f"http://127.0.0.1:{cdp_port}/json", timeout=3)
        response.raise_for_status()
        tabs = response.json()
        if not isinstance(tabs, list):
            raise RuntimeError("CDP /json did not return a page list")
        ws_url = _select_exact_page(tabs, website_url)
    except Exception as exc:
        raise RuntimeError(f"Cannot bind exact Chrome CDP page on port {cdp_port}: {exc}") from exc

    stop_event = threading.Event()
    mouse_thread = threading.Thread(target=_fake_mouse_routine, args=(ws_url, regimes_path, stop_event), daemon=True)
    mouse_thread.start()
    try:
        solution_token = solve_captcha(website_url, website_key, captcha_type)
    finally:
        stop_event.set()
        mouse_thread.join(timeout=5)
        if mouse_thread.is_alive():
            log.error("[FakeMouse] helper did not exit within 5s; daemon thread will not block worker shutdown")

    if solution_token:
        _inject_solution(ws_url, solution_token, captcha_type)
    return solution_token
