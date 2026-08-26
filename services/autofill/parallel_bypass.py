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


def _page_url(ws) -> str:
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "location.href", "returnByValue": True}}))
    response = json.loads(ws.recv())
    return str(response.get("result", {}).get("result", {}).get("value") or "")




def _validate_unique_linkedin_ws_target(ws_url: str, current_url: str) -> None:
    """Fail closed when the frozen caller picked a non-unique LinkedIn page."""
    parsed_ws = urlsplit(ws_url)
    host = parsed_ws.hostname or "127.0.0.1"
    port = parsed_ws.port
    if not port:
        return
    try:
        response = requests.get(f"http://{host}:{port}/json", timeout=2)
        response.raise_for_status()
        tabs = response.json()
    except Exception as exc:
        raise RuntimeError(f"cannot validate CDP target list: {exc}") from exc
    linkedin = [tab for tab in tabs if isinstance(tab, dict) and tab.get("type") == "page"
                and ((urlsplit(str(tab.get("url") or "")).hostname or "").casefold().endswith("linkedin.com"))]
    if len(linkedin) != 1 or str(linkedin[0].get("webSocketDebuggerUrl") or "") != ws_url:
        raise RuntimeError(
            f"frozen caller target is ambiguous: expected one exact LinkedIn page, found {len(linkedin)}; "
            "refocus/close extra LinkedIn tabs before retrying"
        )

def _fake_mouse_routine(ws_url: str, regimes_file_path: str, stop_event: threading.Event):
    """Bounded CDP mouse routine. Never blocks the worker indefinitely."""
    ws = None
    try:
        log.info("[FakeMouse] Connecting to CDP websocket...")
        ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=3)
        ws.settimeout(3)
        current_url = _page_url(ws)
        # Frozen LinkedIn callers do not pass an exact URL. Refuse to emit into
        # an unrelated tab rather than spraying CDP input into the first page.
        host = (urlsplit(current_url).hostname or "").casefold()
        if host not in {"linkedin.com", "www.linkedin.com"} and not host.endswith(".linkedin.com"):
            log.warning("[FakeMouse] Refusing non-LinkedIn target: %s", current_url)
            return
        _validate_unique_linkedin_ws_target(ws_url, current_url)

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

        with open(regimes_file_path, "r", encoding="utf-8") as f:
            regimes = json.load(f).get("regimes", [])
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
        log.error("[FakeMouse] bounded helper stopped: %s", exc)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


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
