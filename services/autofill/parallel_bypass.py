import threading
import time
import json
import random
import itertools
import requests
import websocket
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit

from services.autofill.capsolver_api import solve_captcha

log = logging.getLogger(__name__)
_CDP_IDS = itertools.count(10000)


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


def _cdp_call(ws, method: str, params: dict | None = None, *, timeout_seconds: float = 3.0) -> dict:
    """Send one CDP command and wait for its matching response, ignoring async events.

    CDP websocket streams can interleave Runtime/Network/Page event messages with
    command responses.  Treating the very next ``recv()`` as our response can
    mis-parse an event as success/failure.  This helper stays bounded while it
    skips unrelated messages until the exact command id arrives.
    """
    call_id = next(_CDP_IDS)
    ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for CDP response to {method}")
        raw = ws.recv()
        message = json.loads(raw)
        if not isinstance(message, dict):
            continue
        if message.get("id") != call_id:
            # Async events and responses for other in-flight commands are not
            # evidence about this call.  This websocket is single-owner, so it
            # is safe to ignore them here.
            continue
        if message.get("error"):
            raise RuntimeError(f"CDP {method} failed: {message['error']}")
        return message


def _page_url(ws) -> str:
    response = _cdp_call(
        ws, "Runtime.evaluate",
        {"expression": "location.href", "returnByValue": True},
    )
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

        size_res = _cdp_call(
            ws, "Runtime.evaluate",
            {"expression": "({width: window.innerWidth, height: window.innerHeight})", "returnByValue": True},
        )
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
                    # A LinkedIn tab can navigate to an employer ATS while
                    # retaining the same CDP target/websocket.  Revalidate the
                    # live URL periodically so the LinkedIn multi-tab helper
                    # never keeps dispatching mouse events after that boundary.
                    if require_linkedin and _step % 10 == 0:
                        live_url = _page_url(ws)
                        if not _is_linkedin_url(live_url):
                            log.info("[FakeMouse] LinkedIn target navigated away; stopping coverage: %s", live_url)
                            return
                    current_x = max(0, min(current_x + (drift_x * dt) + random.gauss(0, 2.0), width))
                    current_y = max(0, min(current_y + (drift_y * dt) + random.gauss(0, 2.0), height))
                    _cdp_call(
                        ws, "Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": int(current_x), "y": int(current_y)},
                    )
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
    tabs: list[dict] = []
    try:
        tabs = _cdp_tabs(ws_url)
        seed = _seed_target(tabs, ws_url)
    except Exception as exc:
        log.warning("[FakeMouse] cannot classify seed target; using exact seed only: %s", exc)
        seed = None

    # Discovery handlers are frozen and historically seed the first page tab.
    # If any LinkedIn page exists, ignore an unrelated first-page seed and fan
    # out only across the live LinkedIn targets. Deterministic autofill no
    # longer calls this helper, so this cannot inject motion into an ATS form.
    if any(_is_linkedin_url(str(tab.get("url") or "")) for tab in tabs if tab.get("type") == "page"):
        _fake_mouse_linkedin_controller(ws_url, regimes_file_path, stop_event)
        return

    _fake_mouse_target_routine(ws_url, regimes_file_path, stop_event, require_linkedin=False)


def _canonical_target_url(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    path = (parsed.path or "/").rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return host, path, query


def _select_exact_page(tabs: list[dict], website_url: str) -> str:
    wanted = _canonical_target_url(website_url)
    candidates = []
    for tab in tabs:
        if tab.get("type") != "page" or not tab.get("webSocketDebuggerUrl"):
            continue
        if _canonical_target_url(str(tab.get("url") or "")) == wanted:
            candidates.append(tab)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one exact CDP page for {website_url!r}; found {len(candidates)}")
    return str(candidates[0]["webSocketDebuggerUrl"])


def _select_exact_target(tabs: list[dict], target_id: str, website_url: str) -> str:
    """Bind one already-authorized CDP target and revalidate its live URL.

    URL-only selection is intentionally retained for backward compatibility,
    but discovery/CAPTCHA callers that already own a stable target must use
    this stronger identity boundary.  Two tabs can legitimately share the
    same LinkedIn URL; a target id is the only unambiguous browser identity.
    """
    wanted_id = str(target_id or "").strip()
    if not wanted_id:
        raise RuntimeError("Expected CDP target id is empty")
    candidates: list[dict] = []
    for tab in tabs:
        if tab.get("type") != "page" or not tab.get("webSocketDebuggerUrl"):
            continue
        aliases = {
            str(tab.get(key) or "").strip()
            for key in ("id", "targetId", "tabId", "suggestedTargetId")
        }
        if wanted_id in aliases:
            candidates.append(tab)
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one CDP page for target {wanted_id!r}; found {len(candidates)}"
        )
    live_url = str(candidates[0].get("url") or "")
    if _canonical_target_url(live_url) != _canonical_target_url(website_url):
        raise RuntimeError(
            "Exact CDP target changed URL before CAPTCHA injection; refusing stale binding"
        )
    return str(candidates[0]["webSocketDebuggerUrl"])


def _inject_solution(
    ws_url: str,
    solution_token: str,
    captcha_type: str,
    *,
    expected_url: str | None = None,
) -> None:
    token_js = json.dumps(str(solution_token))
    ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=3)
    try:
        ws.settimeout(3)
        if expected_url is not None:
            live_url = _page_url(ws)
            if _canonical_target_url(live_url) != _canonical_target_url(expected_url):
                raise RuntimeError(
                    "Exact CAPTCHA target changed URL while awaiting the solver; refusing stale injection"
                )
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
        response = _cdp_call(
            ws, "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        value = response.get("result", {}).get("result", {}).get("value") or {}
        if not value.get("ok"):
            raise RuntimeError(f"captcha token injection could not bind to the expected DOM control: {value}")
    finally:
        ws.close()


def execute_parallel_bypass(cdp_port: int, website_url: str, website_key: str, regimes_path: str,
                            captcha_type: str = "ReCaptchaV2TaskProxyLess",
                            *, target_id: str | None = None):
    """Bounded orchestrator preserving the existing CapSolver/FakeMouse feature."""
    try:
        response = requests.get(f"http://127.0.0.1:{cdp_port}/json", timeout=3)
        response.raise_for_status()
        tabs = response.json()
        if not isinstance(tabs, list):
            raise RuntimeError("CDP /json did not return a page list")
        ws_url = (
            _select_exact_target(tabs, target_id, website_url)
            if target_id
            else _select_exact_page(tabs, website_url)
        )
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
        _inject_solution(
            ws_url,
            solution_token,
            captcha_type,
            expected_url=website_url,
        )
    return solution_token
