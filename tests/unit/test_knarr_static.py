"""Tests for knarr-static system skill: deploy, undeploy, list, security."""
import os
import tempfile
import zipfile
import pytest

from knarr.static.handler import (
    handle, set_node, _deployments, _get_static_root,
    MAX_PATH_LENGTH, MAX_FILES
)


class MockNode:
    """Minimal mock of DHTNode for knarr-static tests."""

    def __init__(self, tmpdir):
        self._config = {"static": {"enabled": True}}
        self._asset_dir = os.path.join(tmpdir, "assets")
        os.makedirs(self._asset_dir, exist_ok=True)
        self.node_info = type("Info", (), {"node_id": "local-node-id"})()
        self._assets = {}

    def get_asset(self, hash):
        if hash in self._assets:
            return self._assets[hash]
        raise FileNotFoundError(f"Asset {hash} not found")

    def store_asset(self, data):
        import hashlib
        h = hashlib.sha256(data).hexdigest()
        self._assets[h] = data
        return h


def _make_zip(files: dict) -> bytes:
    """Create an in-memory zip from {filename: content} dict."""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def static_env(tmp_path):
    """Set up knarr-static with a mock node."""
    node = MockNode(str(tmp_path))
    _deployments.clear()
    set_node(node)
    return node


class TestDeploy:
    def test_deploy_valid_archive(self, static_env):
        zip_data = _make_zip({"index.html": "<h1>Hello</h1>", "style.css": "body{}"})
        archive_hash = static_env.store_asset(zip_data)

        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "myapp",
            "archive": archive_hash,
        })
        assert result["status"] == "deployed"
        assert result["files"] == 2
        assert result["url"] == "/s/myapp/"

    def test_deploy_missing_index_html(self, static_env):
        zip_data = _make_zip({"app.js": "console.log('hi')"})
        archive_hash = static_env.store_asset(zip_data)

        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "myapp",
            "archive": archive_hash,
        })
        assert result["error"] == "missing_index"

    def test_deploy_path_traversal_rejected(self, static_env):
        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "../etc/passwd",
            "archive": "a" * 64,
        })
        assert result["error"] == "path_traversal"

    def test_deploy_invalid_path_chars(self, static_env):
        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "my app!",
            "archive": "a" * 64,
        })
        assert result["error"] == "invalid_path"

    def test_deploy_path_too_long(self, static_env):
        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "a" * (MAX_PATH_LENGTH + 1),
            "archive": "a" * 64,
        })
        assert result["error"] == "invalid_path"

    def test_deploy_invalid_archive_hash(self, static_env):
        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "myapp",
            "archive": "not-a-hash",
        })
        assert result["error"] == "invalid_archive"

    def test_deploy_bad_zip(self, static_env):
        archive_hash = static_env.store_asset(b"not a zip file at all")

        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "myapp",
            "archive": archive_hash,
        })
        assert result["error"] == "bad_archive"


class TestLocalOnly:
    def test_remote_caller_rejected(self, static_env):
        result = handle({
            "_caller_node_id": "remote-attacker",
            "action": "list",
        })
        assert result["error"] == "local_only"

    def test_no_caller_rejected(self, static_env):
        result = handle({
            "action": "list",
        })
        assert result["error"] == "local_only"


class TestUndeploy:
    def test_undeploy_existing(self, static_env):
        zip_data = _make_zip({"index.html": "<h1>Hi</h1>"})
        archive_hash = static_env.store_asset(zip_data)

        handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "toremove",
            "archive": archive_hash,
        })

        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "undeploy",
            "path": "toremove",
        })
        assert result["status"] == "undeployed"

    def test_undeploy_nonexistent(self, static_env):
        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "undeploy",
            "path": "doesnotexist",
        })
        assert result["error"] == "not_found"


class TestList:
    def test_list_empty(self, static_env):
        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "list",
        })
        assert result["count"] == 0
        assert result["sites"] == []

    def test_list_after_deploy(self, static_env):
        zip_data = _make_zip({"index.html": "<h1>Hi</h1>"})
        archive_hash = static_env.store_asset(zip_data)

        handle({
            "_caller_node_id": "local-node-id",
            "action": "deploy",
            "path": "site1",
            "archive": archive_hash,
        })

        result = handle({
            "_caller_node_id": "local-node-id",
            "action": "list",
        })
        assert result["count"] == 1
        assert result["sites"][0]["path"] == "site1"
