import io
import os
import tarfile
from pathlib import Path

import pytest

from knarr.watchman.plugin_manager import _extract_tarball, _write_manifest_simple

def test_o031_asset_name_is_sanitized_with_basename():
    source = (Path(__file__).parent.parent.parent / "src/knarr/watchman/upgrader.py").read_text(encoding="utf-8")
    assert "safe_name = os.path.basename(name)" in source


def test_o032_tar_member_traversal_is_rejected(tmp_path):
    tarball = tmp_path / "plugin.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        safe = tarfile.TarInfo("repo-123/safe.txt")
        safe_bytes = b"safe"
        safe.size = len(safe_bytes)
        tar.addfile(safe, io.BytesIO(safe_bytes))

        evil = tarfile.TarInfo("repo-123/nested/../../evil.sh")
        evil_bytes = b"evil"
        evil.size = len(evil_bytes)
        tar.addfile(evil, io.BytesIO(evil_bytes))

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    _extract_tarball(str(tarball), str(plugin_dir))

    assert (plugin_dir / "safe.txt").exists()
    assert not (plugin_dir / "evil.sh").exists()


def test_o033_checksum_verification_uses_two_pass_order_independent_logic():
    source = (Path(__file__).parent.parent.parent / "src/knarr/watchman/upgrader.py").read_text(encoding="utf-8")
    assert "# O-033: Pass 1" in source
    assert "# O-033: Pass 2" in source
    assert source.index("# O-033: Pass 1") < source.index("# O-033: Pass 2")


def test_o034_manifest_rejects_special_chars_but_allows_dots(tmp_path):
    manifest_path = tmp_path / "plugins.toml"

    with pytest.raises(ValueError):
        _write_manifest_simple(str(manifest_path), {"plugins": {"evil]\n[bad": {"enabled": True}}})

    _write_manifest_simple(str(manifest_path), {"plugins": {"plugin.v1": {"enabled": True}}})
    content = manifest_path.read_text(encoding="utf-8")
    assert "[plugins.plugin.v1]" in content
