"""Regression tests for the user-owned fixed resume project registry."""
import json
from pathlib import Path
import subprocess
import sys

from services.common import project_registry


def test_registry_keeps_the_six_immutable_template_blocks(tmp_path: Path):
    registry = project_registry.empty_registry()
    registry["projects"][0]["template_title"] = "CAROECT-D — verified template title"
    registry["projects"][0]["asset_title_aliases"].append("Roadside event camera project")
    path = project_registry.save_registry(registry, tmp_path / "projects.json")
    loaded = project_registry.load_registry(path)

    assert len(loaded["projects"]) == 6
    assert loaded["projects"][0]["resume_slot_start"] == 1
    assert "Roadside event camera project" in loaded["projects"][0]["asset_title_aliases"]


def test_registry_rejects_renaming_or_adding_resume_blocks(tmp_path: Path):
    registry = project_registry.empty_registry()
    registry["projects"][0]["display_name"] = "A different title"
    try:
        project_registry.save_registry(registry, tmp_path / "invalid.json")
    except project_registry.ProjectRegistryError as exc:
        assert "cannot rename" in str(exc)
    else:
        raise AssertionError("The user JSON must not change a Word-template project block")


def test_parsed_record_maps_only_when_a_verified_alias_matches():
    registry = project_registry.empty_registry()
    mapped = project_registry.map_parsed_profile_record(
        {"asset_title": "PKI-Sentinel project evidence", "project_tags": ["pki"]}, registry
    )
    unknown = project_registry.map_parsed_profile_record({"asset_title": "Unrelated course"}, registry)

    assert mapped == {
        "project_id": "pki_sentinel", "resume_slot_start": 5,
        "confidence": "alias_match", "matched_aliases": ["pki-sentinel"],
    }
    assert unknown["confidence"] == "unmapped"


def test_mapping_cli_adds_non_destructive_mapping(tmp_path: Path):
    input_path, output_path = tmp_path / "records.json", tmp_path / "mapped.json"
    input_path.write_text(json.dumps([{"asset_title": "CIG-AMF research profile", "other": 1}]), encoding="utf-8")
    script = Path(__file__).parent / "scripts" / "jobos_project_profile_app.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--path", str(tmp_path / "projects.json"),
         "--map-input", str(input_path), "--map-output", str(output_path)],
        text=True, capture_output=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(output_path.read_text(encoding="utf-8"))[0]["jobos_project_mapping"]["project_id"] == "cig_amf"
