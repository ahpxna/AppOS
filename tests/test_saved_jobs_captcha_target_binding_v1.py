from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from services.autofill import parallel_bypass


ROOT = Path(__file__).resolve().parents[1]


def _load_worker():
    path = ROOT / "services" / "browser-controller" / "browser_queue_worker.py"
    spec = importlib.util.spec_from_file_location("saved_jobs_captcha_binding_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tab(target_id: str, url: str, opener_id: str = "") -> dict[str, str]:
    return {
        "target_id": target_id,
        "url": url,
        "opener_id": opener_id,
        "websocket_url": f"ws://127.0.0.1/devtools/page/{target_id}",
    }


def test_saved_jobs_captcha_ignores_preexisting_linkedin_tabs(monkeypatch):
    worker = _load_worker()
    catalog = {
        "task-source": _tab("task-source", "https://www.linkedin.com/checkpoint/challenge"),
        **{
            f"old-{index}": _tab(f"old-{index}", "https://www.linkedin.com/checkpoint/challenge")
            for index in range(15)
        },
    }
    monkeypatch.setattr(worker, "_linkedin_page_catalog", lambda: catalog)
    monkeypatch.setattr(
        worker,
        "_linkedin_challenge_evidence",
        lambda tab: "/checkpoint/" in tab["url"],
    )

    bound = worker._current_linkedin_page_target(
        "https://www.linkedin.com/my-items/saved-jobs/",
        source_target_id="task-source",
        pre_attempt_target_ids={"task-source", *(f"old-{index}" for index in range(15))},
    )
    assert bound["target_id"] == "task-source"


def test_saved_jobs_captcha_can_bind_unique_new_descendant(monkeypatch):
    worker = _load_worker()
    catalog = {
        "task-source": _tab("task-source", "https://www.linkedin.com/my-items/saved-jobs/"),
        "task-child": _tab(
            "task-child", "https://www.linkedin.com/challenge/", opener_id="task-source"
        ),
        "old-challenge": _tab("old-challenge", "https://www.linkedin.com/checkpoint/challenge"),
    }
    monkeypatch.setattr(worker, "_linkedin_page_catalog", lambda: catalog)
    monkeypatch.setattr(
        worker,
        "_linkedin_challenge_evidence",
        lambda tab: "/challenge" in tab["url"],
    )

    bound = worker._current_linkedin_page_target(
        "https://www.linkedin.com/my-items/saved-jobs/",
        source_target_id="task-source",
        pre_attempt_target_ids={"task-source", "old-challenge"},
    )
    assert bound["target_id"] == "task-child"


def test_saved_jobs_captcha_refuses_unrelated_only_candidate(monkeypatch):
    worker = _load_worker()
    catalog = {
        "task-source": _tab("task-source", "https://www.linkedin.com/my-items/saved-jobs/"),
        "old-challenge": _tab("old-challenge", "https://www.linkedin.com/checkpoint/challenge"),
    }
    monkeypatch.setattr(worker, "_linkedin_page_catalog", lambda: catalog)
    monkeypatch.setattr(
        worker,
        "_linkedin_challenge_evidence",
        lambda tab: "/checkpoint/" in tab["url"],
    )

    with pytest.raises(worker.TransientTaskError, match="task-owned"):
        worker._current_linkedin_page_target(
            "https://www.linkedin.com/my-items/saved-jobs/",
            source_target_id="task-source",
            pre_attempt_target_ids={"task-source", "old-challenge"},
        )


def test_task_source_maps_openclaw_alias_to_unique_new_cdp_target(monkeypatch):
    worker = _load_worker()
    task: dict = {"timeout_seconds": 30}
    catalogs = iter(
        [
            {"old": _tab("old", "https://www.linkedin.com/jobs/view/1/")},
            {
                "old": _tab("old", "https://www.linkedin.com/jobs/view/1/"),
                "CDP-FULL-ID": _tab(
                    "CDP-FULL-ID", "https://www.linkedin.com/my-items/saved-jobs/"
                ),
            },
        ]
    )

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def open(self, _url):
            return type("Target", (), {"target_id": "openclaw-short-id"})()

        def current_url(self, _target_id):
            return "https://www.linkedin.com/my-items/saved-jobs/"

        def close(self, _target_id):
            return None

    monkeypatch.setattr(worker, "OpenClawTransport", Transport)
    monkeypatch.setattr(worker, "_linkedin_page_catalog", lambda: next(catalogs))

    cdp_id, openclaw_id, baseline = worker._prepare_linkedin_task_target(
        task, "https://www.linkedin.com/my-items/saved-jobs/"
    )
    assert cdp_id == "CDP-FULL-ID"
    assert openclaw_id == "openclaw-short-id"
    assert baseline == {"old", "CDP-FULL-ID"}
    assert task["_owned_linkedin_target_ids"] == ["openclaw-short-id"]


def test_parallel_bypass_exact_target_does_not_fall_back_to_same_url():
    tabs = [
        {
            "id": "wrong",
            "type": "page",
            "url": "https://www.linkedin.com/checkpoint/challenge",
            "webSocketDebuggerUrl": "ws://wrong",
        },
        {
            "id": "right",
            "type": "page",
            "url": "https://www.linkedin.com/checkpoint/challenge",
            "webSocketDebuggerUrl": "ws://right",
        },
    ]
    assert parallel_bypass._select_exact_target(
        tabs, "right", "https://www.linkedin.com/checkpoint/challenge"
    ) == "ws://right"
    with pytest.raises(RuntimeError, match="changed URL"):
        parallel_bypass._select_exact_target(
            tabs, "right", "https://www.linkedin.com/challenge/different"
        )


def test_captcha_injection_revalidates_url_after_solver_wait(monkeypatch):
    class Socket:
        def settimeout(self, _timeout):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        parallel_bypass.websocket,
        "create_connection",
        lambda *_args, **_kwargs: Socket(),
    )
    monkeypatch.setattr(
        parallel_bypass,
        "_page_url",
        lambda _ws: "https://www.linkedin.com/jobs/view/999/",
    )
    with pytest.raises(RuntimeError, match="changed URL while awaiting"):
        parallel_bypass._inject_solution(
            "ws://right",
            "secret-token",
            "FunCaptchaTaskProxyless",
            expected_url="https://www.linkedin.com/checkpoint/challenge",
        )
