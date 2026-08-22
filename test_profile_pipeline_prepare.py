"""Transaction-wrapper tests for deterministic profile preparation."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PATH = ROOT / "services" / "profile-ingestion" / "prepare_profile_for_pipeline_v1.py"
SPEC = importlib.util.spec_from_file_location("jobos_profile_prepare_test", PATH)
prepare = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(prepare)


def test_builder_wrapper_is_removed_so_both_builders_share_one_transaction():
    sql = "-- comment\nBEGIN;\nCREATE TEMP TABLE unit_test(id int);\nCOMMIT;\n"
    stripped = prepare.strip_outer_transaction(sql, Path("unit.sql"))

    assert "BEGIN;" not in stripped
    assert "COMMIT;" not in stripped
    assert "CREATE TEMP TABLE" in stripped


def test_builder_wrapper_must_be_explicit_to_prevent_accidental_commits():
    try:
        prepare.strip_outer_transaction("SELECT 1;", Path("bad.sql"))
    except RuntimeError as exc:
        assert "outer BEGIN/COMMIT" in str(exc)
    else:
        raise AssertionError("Expected strict wrapper validation")
