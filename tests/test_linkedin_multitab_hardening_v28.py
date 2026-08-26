import threading


def test_linkedin_seed_fans_out_but_non_linkedin_seed_stays_exact(monkeypatch):
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
    assert calls[1][:3] == ("exact", ats_seed, "regimes.json")
    assert calls[1][-1] is False


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
        {"type": "page", "url": "https://www.linkedin.com/jobs/search/", "webSocketDebuggerUrl": "ws://search"},
        {"type": "page", "url": "https://www.linkedin.com/jobs/view/1", "webSocketDebuggerUrl": "ws://detail"},
    ]
    assert parallel_bypass._select_exact_page(
        tabs, "https://www.linkedin.com/jobs/search/?keywords=security"
    ) == "ws://search"
