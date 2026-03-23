"""ESC-01: Decouple skill-sheet from validation success.

Tests that _own_skills is populated from raw data before validation runs,
so the skill handler is callable even when validation fails.
"""
import sys
import os
import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from knarr.core.validation import ValidationError
from knarr.core.models import SkillSheet


def _make_skill_data(**overrides):
    """Valid base skill sheet data."""
    d = {
        "name": "test-skill",
        "version": "1.0.0",
        "description": "A test skill for ESC-01",
        "tags": ["test"],
        "input_schema": {"query": "string"},
        "output_schema": {"result": "string"},
    }
    d.update(overrides)
    return d


def _make_fake_node():
    """Minimal fake node with just enough for announce() to run."""
    from knarr.core.models import NodeInfo

    node = MagicMock()
    node.node_info = NodeInfo(node_id="a" * 64, host="127.0.0.1", port=9000)
    node._own_skills = {}
    node._node_jurisdiction_wire = None
    node._skill_visibility = {}
    node._signing_key = None
    node._sidecar_port = 0
    node._encryption_key_hex = ""
    node._wallet = ""
    node._gossip_fanout = 0
    node.bus = None
    node._public_key_hex = ""

    # _enqueue_write must be awaitable
    node._enqueue_write = AsyncMock()
    node._get_skill_ttl = MagicMock(return_value=3600)
    node._update_meta_cache = MagicMock()
    node.refresh_node_meta = MagicMock()
    node.storage = MagicMock()
    node.storage.get_peers = MagicMock(return_value=[])

    def _sign(msg):
        signed = MagicMock()
        signed.public_key = ""
        signed.signature = ""
        signed.msg_id = "test-id"
        signed.sidecar_port = 0
        return signed

    node._sign = _sign
    return node


def test_invalid_skill_handler_callable_not_in_dht():
    """Skill registered with invalid field: handler callable, not in DHT.

    Simulate validation failure. ESC-01 requires _own_skills populated from raw data
    before validation runs.
    """
    from knarr.dht.node import DHTNode

    node = _make_fake_node()
    skill_data = _make_skill_data(name="my-skill")

    with patch("knarr.dht.node.validate_skill_sheet") as mock_validate:
        mock_validate.side_effect = ValidationError("simulated: invalid field")

        with pytest.raises(ValidationError):
            asyncio.run(DHTNode.announce(node, skill_data))

    # _own_skills populated from raw data before validation ran
    assert "my-skill" in node._own_skills, (
        "_own_skills must be populated even when validation fails"
    )
    # No storage write for invalid skill (not DHT-discoverable)
    node._enqueue_write.assert_not_called()


def test_valid_skill_handler_callable_and_in_dht():
    """Skill registered with valid fields: handler callable and in DHT."""
    from knarr.dht.node import DHTNode

    node = _make_fake_node()
    skill_data = _make_skill_data(name="good-skill")

    asyncio.run(DHTNode.announce(node, skill_data))

    assert "good-skill" in node._own_skills, (
        "_own_skills must contain the validated SkillSheet"
    )
    # Storage write enqueued (DHT-discoverable)
    node._enqueue_write.assert_called_once()


def test_validated_sheet_overwrites_raw():
    """Validated SkillSheet overwrites the raw pre-populated entry."""
    from knarr.dht.node import DHTNode

    node = _make_fake_node()
    skill_data = _make_skill_data(name="overwrite-me", price=5.0)

    asyncio.run(DHTNode.announce(node, skill_data))

    sheet = node._own_skills.get("overwrite-me")
    assert isinstance(sheet, SkillSheet)
    assert sheet.price == 5.0
