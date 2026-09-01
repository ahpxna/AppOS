from __future__ import annotations

import subprocess

import scripts.start_openclaw_jobos as start


def test_health_fails_when_openclaw_process_status_lies(monkeypatch, capsys):
    monkeypatch.setattr(start, "runtime_env", lambda: {})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="Connectivity probe: failed\n"
        ),
    )
    monkeypatch.setattr(start.sys, "argv", ["start_openclaw_jobos.py", "health"])
    assert start.main() == 1
    assert "Connectivity probe: failed" in capsys.readouterr().out


def test_health_accepts_semantic_connectivity_success(monkeypatch):
    monkeypatch.setattr(start, "runtime_env", lambda: {})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="Connectivity probe: ok\n"
        ),
    )
    monkeypatch.setattr(start.sys, "argv", ["start_openclaw_jobos.py", "health"])
    assert start.main() == 0
