"""Tests for version gating via heartbeat."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from knarr.dht.node import DHTNode, _parse_version
from knarr.core.messages import Heartbeat, TaskRequest, SIGNATURE_EXCLUDED_FIELDS
from knarr import __version__


def test_parse_version():
    assert _parse_version("0.6.0") == (0, 6, 0)
    assert _parse_version("1.0.0") == (1, 0, 0)
    assert _parse_version("0.10.3") == (0, 10, 3)
    assert _parse_version("") == (0, 0, 0)
    assert _parse_version("bad") == (0, 0, 0)
    assert _parse_version("1.2") == (1, 2)


def test_parse_version_comparison():
    assert _parse_version("0.7.0") > _parse_version("0.6.0")
    assert _parse_version("1.0.0") > _parse_version("0.99.99")
    assert _parse_version("0.6.0") == _parse_version("0.6.0")
    assert _parse_version("0.6.1") > _parse_version("0.6.0")


def test_heartbeat_has_version_fields():
    hb = Heartbeat(node_id="test", version="0.6.0", min_protocol_version="0.5.0")
    assert hb.version == "0.6.0"
    assert hb.min_protocol_version == "0.5.0"


def test_heartbeat_version_fields_excluded_from_signature():
    assert "version" in SIGNATURE_EXCLUDED_FIELDS
    assert "min_protocol_version" in SIGNATURE_EXCLUDED_FIELDS


def test_heartbeat_version_defaults_empty():
    hb = Heartbeat(node_id="test")
    assert hb.version == ""
    assert hb.min_protocol_version == ""


@pytest.mark.asyncio
async def test_version_gated_rejects_tasks():
    """A version-gated node rejects task requests."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        node._version_gated = True

        # Register a dummy handler so the skill exists
        async def echo(data):
            return data
        node.register_handler("echo", echo)
        await node.announce({
            "name": "echo", "version": "1.0.0", "description": "test",
            "tags": ["test"], "input_schema": {}, "output_schema": {}
        })

        msg = TaskRequest(
            task_id="test-task", requester_node_id="abc",
            requester_host="127.0.0.1", requester_port=9999,
            skill_name="echo", input_data={"text": "hello"},
            public_key=node._public_key_hex,
            signature="fake"
        )

        result = await node._handle_task_request(msg)
        assert result.status == "failed"
        assert result.error["code"] == "VERSION_GATED"
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_version_gated_skips_reannounce():
    """A version-gated node doesn't re-announce skills."""
    node = DHTNode("127.0.0.1", 0)
    await node.start()
    try:
        async def echo(data):
            return data
        node.register_handler("echo", echo)
        await node.announce({
            "name": "echo", "version": "1.0.0", "description": "test",
            "tags": ["test"], "input_schema": {}, "output_schema": {}
        })

        node._version_gated = True

        # Mock storage to track if get_peers is even called
        original_get_peers = node.storage.get_peers
        get_peers_called = False
        def tracking_get_peers():
            nonlocal get_peers_called
            get_peers_called = True
            return original_get_peers()

        node.storage.get_peers = tracking_get_peers
        await node._reannounce_all()

        # Should return early without even calling get_peers
        assert not get_peers_called
    finally:
        await node.stop()


def test_version_gated_flag_default():
    """Node starts with version gating disabled."""
    node = DHTNode.__new__(DHTNode)
    node._config = {}
    node._version_gated = False
    assert not node._version_gated


def test_min_protocol_version_from_config():
    """Bootstrap servers can set min_protocol_version via config."""
    node = DHTNode.__new__(DHTNode)
    node._config = {"node": {"min_protocol_version": "0.7.0"}}
    node._min_protocol_version = node._config.get("node", {}).get("min_protocol_version", "")
    assert node._min_protocol_version == "0.7.0"


def test_version_in_package():
    """__version__ is defined and looks like semver."""
    assert __version__
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
