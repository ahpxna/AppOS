from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "jobos_profile_ready.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("jobos_profile_ready_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_roots(module, root: Path) -> None:
    module.ROOT = root
    module.RAW_ROOT = root / "data" / "profile_raw"
    module.SOURCE_ROOT = root / "data" / "profile_sources_v2"
    module.PARSED_ROOT = root / "data" / "profile_parsed_v2"
    module.MANIFEST_PATH = module.SOURCE_ROOT / ".jobos_profile_ready_manifest.json"
    module.RAW_ROOT.mkdir(parents=True, exist_ok=True)


def test_drop_folder_staging_is_idempotent_replace_guarded_and_prunable(tmp_path: Path):
    runner = load_runner()
    configure_roots(runner, tmp_path)

    resume = runner.RAW_ROOT / "resume.txt"
    project = runner.RAW_ROOT / "project" / "demo.md"
    project.parent.mkdir(parents=True)
    resume.write_text("synthetic resume v1", encoding="utf-8")
    project.write_text("# synthetic project\nfixture only", encoding="utf-8")

    first, removed = runner.stage_raw_documents(replace=False)
    assert not removed
    assert {item["action"] for item in first} == {"copied"}
    assert {item["bucket"] for item in first} == {"00_official", "02_project_profiles"}

    second, removed = runner.stage_raw_documents(replace=False)
    assert not removed
    assert all(item["action"] == "unchanged" for item in second)

    resume.write_text("synthetic resume v2", encoding="utf-8")
    with pytest.raises(runner.PipelineError, match="Staging conflict"):
        runner.stage_raw_documents(replace=False)

    replaced, _ = runner.stage_raw_documents(replace=True)
    assert any(item["action"] == "replaced" for item in replaced)

    parsed_sidecar = runner.PARSED_ROOT / "02_project_profiles" / "demo.txt"
    parsed_sidecar.parent.mkdir(parents=True, exist_ok=True)
    parsed_sidecar.write_text("old parsed fixture", encoding="utf-8")
    project.unlink()

    current, removed = runner.stage_raw_documents(replace=True)
    assert len(current) == 1
    assert len(removed) == 1 and removed[0]["action"] == "removed"
    assert not (runner.SOURCE_ROOT / "02_project_profiles" / "demo.md").exists()
    assert not parsed_sidecar.exists()


def test_run_checks_raw_documents_before_dependency_bootstrap(tmp_path: Path, monkeypatch, capsys):
    runner = load_runner()
    configure_roots(runner, tmp_path)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--no-install", "run"])

    def should_not_run(*args, **kwargs):
        raise AssertionError("dependency bootstrap must not run for an empty profile_raw")

    monkeypatch.setattr(runner, "ensure_venv_and_reexec", should_not_run)
    assert runner.main() == 1
    err = capsys.readouterr().err
    assert "No supported documents found" in err


def test_changed_staged_sources_force_reparse_contract():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'force_parse=any(item["action"] != "unchanged" for item in staged)' in source
    assert 'parse_cmd.append("--force")' in source
    assert "staged_sources_synced(staged)" in source
