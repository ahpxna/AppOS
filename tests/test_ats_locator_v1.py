from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "services" / "discovery" / "ats_discovery_v1.py"
_spec = importlib.util.spec_from_file_location("ats_discovery_locator_test", MODULE)
assert _spec and _spec.loader
ats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ats)


def test_native_adapter_requires_real_tenant_key():
    platform, slug, source_url = ats._validated_company_locator("greenhouse", "acme", None)
    assert (platform, slug, source_url) == ("greenhouse", "acme", None)
    try:
        ats._validated_company_locator("greenhouse", None, "https://boards.greenhouse.io/acme")
    except ats.DiscoveryError as exc:
        assert "requires --slug" in str(exc)
    else:
        raise AssertionError("native adapter accepted a missing tenant key")


def test_structured_adapter_requires_source_url_not_fake_slug():
    platform, slug, source_url = ats._validated_company_locator(
        "workday", None, "https://acme.wd5.myworkdayjobs.com/en-US/jobs"
    )
    assert platform == "workday"
    assert slug is None
    assert source_url.startswith("https://acme.wd5.myworkdayjobs.com/")
    try:
        ats._validated_company_locator("workday", "invented", None)
    except ats.DiscoveryError as exc:
        assert "requires --source-url" in str(exc)
    else:
        raise AssertionError("structured adapter accepted a fake slug without source URL")
