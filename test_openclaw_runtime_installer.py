import io
import os
import tarfile
import tempfile
from pathlib import Path

import services.common.openclaw_runtime as runtime
from scripts.install_openclaw_runtime import RuntimeInstallError, safe_extract


def _archive(path: Path, link_target: str) -> None:
    with tarfile.open(path, "w:xz") as tar:
        data = b"node"
        item = tarfile.TarInfo("node-v24/bin/node")
        item.size = len(data)
        tar.addfile(item, io.BytesIO(data))
        link = tarfile.TarInfo("node-v24/bin/npm")
        link.type = tarfile.SYMTYPE
        link.linkname = link_target
        tar.addfile(link)


def test_safe_extract_allows_internal_node_style_symlink():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "node.tar.xz"
        _archive(archive, "node")
        target = root / "extract"
        target.mkdir()
        safe_extract(archive, target)
        assert (target / "node-v24/bin/npm").is_symlink()


def test_safe_extract_rejects_symlink_escape():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "node.tar.xz"
        # ``node-v24/bin`` needs three parent traversals to leave the
        # extraction root; two would legitimately resolve to ``extract``.
        _archive(archive, "../../../outside")
        target = root / "extract"
        target.mkdir()
        try:
            safe_extract(archive, target)
        except RuntimeInstallError:
            return
        raise AssertionError("unsafe archive link was accepted")


def test_empty_openclaw_bin_prefers_private_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "openclaw"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        original = runtime.PRIVATE_OPENCLAW_BIN
        old_env = os.environ.get("OPENCLAW_BIN")
        try:
            runtime.PRIVATE_OPENCLAW_BIN = binary
            os.environ["OPENCLAW_BIN"] = ""
            assert runtime.resolve_openclaw_binary() == str(binary)
        finally:
            runtime.PRIVATE_OPENCLAW_BIN = original
            if old_env is None:
                os.environ.pop("OPENCLAW_BIN", None)
            else:
                os.environ["OPENCLAW_BIN"] = old_env
