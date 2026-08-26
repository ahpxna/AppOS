import threading


def test_arbitrary_frozen_handler_seed_fans_out_to_live_linkedin_tabs(monkeypatch):
    from services.autofill import parallel_bypass

    linkedin_seed = "ws://127.0.0.1:9222/devtools/page/linkedin"
    ats_seed = "ws://127.0.0.1:9222/devtools/page/ats"
    tabs = [
        {"type": "page", "url": "https://www.linkedin.com/jobs/search/", "webSocketDebuggerUrl": linkedin_seed},
        {"type": "page", "url": "https://ats.example/app", "webSocketDebuggerUrl": ats_seed},
    ]
    monkeypatch.setattr(parallel_bypass, "_cdp_tabs", lambda _ws: tabs)

    calls = []
    monkeypatch.setattr(
        parallel_bypass,
        "_fake_mouse_linkedin_controller",
        lambda ws, regimes, stop: calls.append(("linkedin", ws, regimes, stop)),
    )
    monkeypatch.setattr(
        parallel_bypass,
        "_fake_mouse_target_routine",
        lambda ws, regimes, stop, *, require_linkedin: calls.append(
            ("exact", ws, regimes, stop, require_linkedin)
        ),
    )

    stop = threading.Event()
    parallel_bypass._fake_mouse_routine(linkedin_seed, "regimes.json", stop)
    parallel_bypass._fake_mouse_routine(ats_seed, "regimes.json", stop)

    assert calls[0][:3] == ("linkedin", linkedin_seed, "regimes.json")
    # Frozen discovery handlers may seed the helper with the first browser
    # page.  If the CDP catalog contains LinkedIn pages, the helper repairs
    # that arbitrary seed by selecting the LinkedIn controller instead of
    # touching the unrelated ATS page.
    assert calls[1][:3] == ("linkedin", ats_seed, "regimes.json")


def test_linkedin_controller_enrolls_all_live_tabs_and_new_tabs(monkeypatch):
    from services.autofill import parallel_bypass

    seed = "ws://127.0.0.1:9222/devtools/page/a"
    tab_a = {"type": "page", "url": "https://www.linkedin.com/jobs/search/", "webSocketDebuggerUrl": seed}
    tab_b = {"type": "page", "url": "https://www.linkedin.com/jobs/view/2", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/b"}
    other = {"type": "page", "url": "https://mail.example/", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/mail"}
    catalogs = iter([[tab_a, other], [tab_a, tab_b, other]])
    monkeypatch.setattr(parallel_bypass, "_cdp_tabs", lambda _ws: next(catalogs))

    started = []

    class FakeThread:
        def __init__(self, *, target, args, kwargs, name, daemon):
            self.target = target
            self.args = args
            self.kwargs = kwargs
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True
            started.append((self.args[0], self.kwargs["require_linkedin"], self.daemon))

        def is_alive(self):
            return self.started

        def join(self, timeout=None):
            assert timeout == 1.0
            self.started = False

    monkeypatch.setattr(parallel_bypass.threading, "Thread", FakeThread)

    class StopAfterTwoRefreshes:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 2

        def wait(self, _seconds):
            self.waits += 1
            return self.is_set()

    parallel_bypass._fake_mouse_linkedin_controller(seed, "regimes.json", StopAfterTwoRefreshes())

    assert started == [
        (seed, True, True),
        ("ws://127.0.0.1:9222/devtools/page/b", True, True),
    ]


def test_captcha_injection_remains_exact_target_even_with_multitab_mouse(monkeypatch):
    from services.autofill import parallel_bypass

    tabs = [
        {"type": "page", "url": "https://www.linkedin.com/jobs/search/?keywords=security", "webSocketDebuggerUrl": "ws://search"},
        {"type": "page", "url": "https://www.linkedin.com/jobs/view/1", "webSocketDebuggerUrl": "ws://detail"},
    ]
    assert parallel_bypass._select_exact_page(
        tabs, "https://www.linkedin.com/jobs/search/?keywords=security"
    ) == "ws://search"


def test_cdp_call_ignores_async_events_until_matching_response(monkeypatch):
    from services.autofill import parallel_bypass

    class FakeWS:
        def __init__(self):
            self.sent = []
            self.messages = []

        def send(self, payload):
            request = __import__("json").loads(payload)
            self.sent.append(request)
            call_id = request["id"]
            self.messages = [
                __import__("json").dumps({"method": "Network.requestWillBeSent", "params": {}}),
                __import__("json").dumps({"id": call_id + 999, "result": {"ignored": True}}),
                __import__("json").dumps({"id": call_id, "result": {"result": {"value": "ok"}}}),
            ]

        def recv(self):
            return self.messages.pop(0)

    ws = FakeWS()
    response = parallel_bypass._cdp_call(
        ws, "Runtime.evaluate", {"expression": "1", "returnByValue": True}
    )
    assert response["result"]["result"]["value"] == "ok"
    assert len(ws.sent) == 1


def test_linkedin_mouse_stops_if_same_target_navigates_to_non_linkedin(monkeypatch):
    from services.autofill import parallel_bypass

    urls = iter([
        "https://www.linkedin.com/jobs/view/1",
        "https://ats.example/apply",
    ])
    monkeypatch.setattr(parallel_bypass, "_page_url", lambda _ws: next(urls))
    monkeypatch.setattr(parallel_bypass, "_load_regimes", lambda _path: [
        {"drift": {"x": 1, "y": 1}}
    ])

    dispatched = []

    def fake_call(_ws, method, params=None, **_kwargs):
        if method == "Runtime.evaluate":
            return {"result": {"result": {"value": {"width": 800, "height": 600}}}}
        if method == "Input.dispatchMouseEvent":
            dispatched.append(params)
            return {"result": {}}
        raise AssertionError(method)

    monkeypatch.setattr(parallel_bypass, "_cdp_call", fake_call)

    class FakeWS:
        def settimeout(self, _value): pass
        def close(self): pass

    monkeypatch.setattr(parallel_bypass.websocket, "create_connection", lambda *_a, **_k: FakeWS())

    class Stop:
        def is_set(self): return False
        def wait(self, _seconds): return False

    parallel_bypass._fake_mouse_target_routine(
        "ws://127.0.0.1:9222/devtools/page/a", "regimes.json", Stop(), require_linkedin=True
    )
    assert dispatched == []
