import importlib.util
from pathlib import Path


_path = Path(__file__).parent / "services" / "repo-audit" / "repository_evidence_v1.py"
_spec = importlib.util.spec_from_file_location("jobos_repository_evidence", _path)
repo_evidence = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(repo_evidence)


def test_inventory_keeps_metadata_but_never_infers_ownership():
    records = repo_evidence.inventory_records({"repos": [{
        "full_name": "candidate/portfolio-app",
        "html_url": "https://github.com/candidate/portfolio-app",
        "clone_url": "https://github.com/candidate/portfolio-app.git",
        "default_branch": "main",
        "private": False,
        "fork": True,
        "archived": False,
        "description": "A portfolio app",
        "homepage": "https://example.test",
        "language": "Python",
        "topics": ["FastAPI", "fastapi", " postgres "],
    }]})

    assert records == [{
        "repo_full_name": "candidate/portfolio-app",
        "canonical_url": "https://github.com/candidate/portfolio-app",
        "clone_url": "https://github.com/candidate/portfolio-app.git",
        "default_branch": "main",
        "revision_sha": None,
        "is_private": False,
        "is_fork": True,
        "archived": False,
        "description": "A portfolio app",
        "homepage": "https://example.test",
        "primary_language": "Python",
        "topics": ["FastAPI", "postgres"],
        "payload": {
            "full_name": "candidate/portfolio-app",
            "html_url": "https://github.com/candidate/portfolio-app",
            "clone_url": "https://github.com/candidate/portfolio-app.git",
            "default_branch": "main",
            "private": False,
            "fork": True,
            "archived": False,
            "description": "A portfolio app",
            "homepage": "https://example.test",
            "language": "Python",
            "topics": ["FastAPI", "fastapi", " postgres "],
        },
    }]


def test_audit_rows_preserve_failed_check_as_evidence_not_success():
    rows = repo_evidence.audit_evidence_rows({"path": "/input/portfolio-app", "checks": [{
        "name": "pytest", "command": ["python", "-m", "pytest", "-q"],
        "exit_code": 1, "stderr": "one test failed",
    }]})
    assert rows[0]["type"] == "audit_check"
    assert "did not pass" in rows[0]["text"]
    assert rows[0]["payload"]["exit_code"] == 1


def test_asset_material_is_conservative_about_repository_claims():
    material = repo_evidence.compile_asset_material({
        "repo_full_name": "candidate/portfolio-app",
        "canonical_url": "https://github.com/candidate/portfolio-app",
        "description": "An experiment",
        "primary_language": "Python",
        "topics": ["FastAPI"],
    }, audit_check_count=1)
    assert "portfolio repository" in material["summary"].casefold()
    assert any("sole authorship" in rule for rule in material["rules"])
    assert any("tests passed" in rule for rule in material["rules"])
