from pathlib import Path

import pytest

from services.runtime.openclaw_runtime import (
    GlobalOpenClawForbiddenError, find_global_openclaw_conflicts,
    inspect_global_openclaw_install, remove_proven_global_openclaw,
)


def test_conflict_scan_finds_every_path_entry_not_only_first(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(); second.mkdir()
    for directory in (first, second):
        binary = directory / "openclaw"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{first}:{second}")
    conflicts = find_global_openclaw_conflicts()
    assert (first / "openclaw").absolute() in conflicts
    assert (second / "openclaw").absolute() in conflicts


def test_unknown_global_binary_is_never_blind_deleted(tmp_path):
    binary = tmp_path / "openclaw"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    install = inspect_global_openclaw_install(binary)
    assert install.removable is False
    with pytest.raises(GlobalOpenClawForbiddenError, match="Refusing to delete"):
        remove_proven_global_openclaw(binary)
    assert binary.exists()


def test_doctor_global_cleanup_enter_keeps_conflict_and_ctrl_c_returns_interrupted(monkeypatch, tmp_path):
    import builtins
    from scripts import jobos
    from services.runtime.openclaw_runtime import GlobalOpenClawInstall

    conflict = (tmp_path / "openclaw").absolute()
    conflict.write_text("#!/bin/sh\nexit 0\n"); conflict.chmod(0o755)
    monkeypatch.setattr(jobos, "find_global_openclaw_conflicts", lambda: [conflict])
    monkeypatch.setattr(jobos, "inspect_global_openclaw_install",
                        lambda path: GlobalOpenClawInstall(path, "unknown standalone/global binary", None))
    monkeypatch.setattr(jobos.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(jobos.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "")
    remaining, interrupted, _detail = jobos._resolve_global_openclaw_conflicts_interactively()
    assert remaining == [conflict] and interrupted is False
    assert conflict.exists()

    def interrupted_input(_prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", interrupted_input)
    remaining, interrupted, _detail = jobos._resolve_global_openclaw_conflicts_interactively()
    assert remaining == [conflict] and interrupted is True
    assert conflict.exists()


def test_doctor_global_cleanup_y_calls_only_proven_removal(monkeypatch, tmp_path):
    import builtins
    from scripts import jobos
    from services.runtime.openclaw_runtime import GlobalOpenClawInstall

    conflict = (tmp_path / "openclaw").absolute()
    conflict.write_text("#!/bin/sh\nexit 0\n"); conflict.chmod(0o755)
    state = {"present": True, "removed": 0}
    monkeypatch.setattr(jobos, "find_global_openclaw_conflicts",
                        lambda: [conflict] if state["present"] else [])
    install = GlobalOpenClawInstall(conflict, "npm global prefix /fixture", ("npm", "uninstall"))
    monkeypatch.setattr(jobos, "inspect_global_openclaw_install", lambda _path: install)
    def remove(_path):
        state["removed"] += 1; state["present"] = False
        return install
    monkeypatch.setattr(jobos, "remove_proven_global_openclaw", remove)
    monkeypatch.setattr(jobos.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(jobos.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "y")
    remaining, interrupted, _detail = jobos._resolve_global_openclaw_conflicts_interactively()
    assert remaining == [] and interrupted is False and state["removed"] == 1
