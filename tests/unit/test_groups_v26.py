"""Tests for v0.26.0 GroupEngine consumer integrations."""
import os
import pytest


# ── Sentinel Tests ────────────────────────────────────────────

def test_sentinel_firewall_group_block_exists():
    """Sentinel: firewall must check block_groups."""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    firewall_path = os.path.join(base_dir, "plugins", "01-firewall", "handler.py")
    with open(firewall_path, "r") as f:
        content = f.read()
    assert "self._block_groups" in content, "Sentinel failed: _block_groups missing in firewall"
    assert "engine.is_member" in content or "self._ctx.group_engine.is_member" in content, \
        "Sentinel failed: group membership check missing"


def test_sentinel_external_signature_required():
    """Sentinel: external HTTPS source must verify signatures."""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    groups_path = os.path.join(base_dir, "plugins", "groups", "handler.py")
    with open(groups_path, "r") as f:
        content = f.read()
    assert "BadSignatureError" in content, "Sentinel failed: external HTTPS must verify signatures"


def test_sentinel_healthcheck_bypass():
    """Sentinel: _healthcheck must bypass input_schema validation."""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    node_path = os.path.join(base_dir, "src", "knarr", "dht", "node.py")
    with open(node_path, "r") as f:
        content = f.read()
    assert "_healthcheck" in content, "Sentinel failed: _healthcheck bypass missing in node.py"


# ── Message Type Tests ────────────────────────────────────────

def test_mail_pull_message_types():
    """MailPull message types exist and are in type_map."""
    from knarr.core.messages import (
        MailPullReq, MailPullResp, MailPullAck, deserialize_message
    )
    import json
    # MailPullReq
    req = MailPullReq(requester_node_id="abc123")
    assert req.type == "MAIL_PULL_REQ"

    # MailPullResp
    resp = MailPullResp(sender_node_id="def456", items=[{"item_id": "i1"}])
    assert resp.type == "MAIL_PULL_RESP"

    # MailPullAck
    ack = MailPullAck(requester_node_id="abc123", item_ids=["i1", "i2"])
    assert ack.type == "MAIL_PULL_ACK"

    # Roundtrip through serialize/deserialize
    from knarr.core.messages import serialize_message
    data = serialize_message(req)
    msg = deserialize_message(data)
    assert isinstance(msg, MailPullReq)
    assert msg.requester_node_id == "abc123"


def test_announce_jurisdiction_field():
    """Announce now carries jurisdiction, and it's in SIGNATURE_EXCLUDED_FIELDS."""
    from knarr.core.messages import Announce, SIGNATURE_EXCLUDED_FIELDS
    msg = Announce(node_id="abc", jurisdiction="eu.ch")
    assert msg.jurisdiction == "eu.ch"
    assert "jurisdiction" in SIGNATURE_EXCLUDED_FIELDS


# ── Pricing Discount Tests ────────────────────────────────────

def _make_node_mock(config, groups):
    """Helper: build a MagicMock node with Storage API returning empty (uses TOML fallback)."""
    from unittest.mock import MagicMock
    node = MagicMock()
    node._config = config
    engine = MagicMock()
    engine.get_groups.return_value = groups
    node._group_engine = engine
    # PRE-01: Return empty from storage.get_discount_rules() so TOML fallback is used
    node.storage.get_discount_rules.return_value = []
    # PRE-01: get_execution_price returns None (no cost data)
    node.storage.get_execution_price.return_value = None
    return node


def test_group_price_discount_single():
    """Single group discount applies correctly."""
    # _calculate_group_price was refactored to _resolve_price_builtin (v0.39.0+)
    from knarr.dht.node import DHTNode
    node = _make_node_mock(
        {"pricing": {"discounts": {"partners": 25}, "min_price": 0.0}},
        ["partners"],
    )
    price, _ = DHTNode._resolve_price_builtin(node, "test_node", 10.0, "test_skill")
    assert price == pytest.approx(7.5)


def test_group_price_discount_stacking():
    """Multiplicative stacking: 25% + 10% = 0.75 * 0.90 = 0.675."""
    from knarr.dht.node import DHTNode
    node = _make_node_mock(
        {"pricing": {"discounts": {"partners": 25, "high_volume": 10}, "min_price": 0.0}},
        ["partners", "high_volume"],
    )
    price, _ = DHTNode._resolve_price_builtin(node, "test_node", 10.0, "test_skill")
    assert price == pytest.approx(6.75)


def test_group_price_floor():
    """Discount can't go below min_price."""
    from knarr.dht.node import DHTNode
    node = _make_node_mock(
        {"pricing": {"discounts": {"partners": 99}, "min_price": 1.0}},
        ["partners"],
    )
    price, _ = DHTNode._resolve_price_builtin(node, "test_node", 10.0, "test_skill")
    assert price == 1.0


# ── Discovery Filter Tests ────────────────────────────────────

def test_discovery_exclude_group():
    """Excluded group members are filtered out."""
    from unittest.mock import MagicMock
    node = MagicMock()
    node._config = {"discovery": {"exclude_groups": ["blocked"]}}
    engine = MagicMock()
    engine.is_member.side_effect = lambda nid, g: nid == "bad_node" and g == "blocked"
    node._group_engine = engine

    from knarr.dht.node import DHTNode
    providers = [
        {"provider_node_id": "good_node"},
        {"provider_node_id": "bad_node"},
    ]
    result = DHTNode._filter_providers_by_group(node, providers)
    assert len(result) == 1
    assert result[0]["provider_node_id"] == "good_node"


def test_discovery_require_group():
    """Only required-group providers returned."""
    from unittest.mock import MagicMock
    node = MagicMock()
    node._config = {"discovery": {"require_groups": ["trusted"]}}
    engine = MagicMock()
    engine.is_member.side_effect = lambda nid, g: nid == "trusted_node" and g == "trusted"
    node._group_engine = engine

    from knarr.dht.node import DHTNode
    providers = [
        {"provider_node_id": "trusted_node"},
        {"provider_node_id": "untrusted_node"},
    ]
    result = DHTNode._filter_providers_by_group(node, providers)
    assert len(result) == 1
    assert result[0]["provider_node_id"] == "trusted_node"


def test_discovery_prefer_group():
    """Preferred providers sorted first."""
    from unittest.mock import MagicMock
    node = MagicMock()
    node._config = {"discovery": {"prefer_groups": ["partners"]}}
    engine = MagicMock()
    engine.is_member.side_effect = lambda nid, g: nid == "partner_node" and g == "partners"
    node._group_engine = engine

    from knarr.dht.node import DHTNode
    providers = [
        {"provider_node_id": "random_node"},
        {"provider_node_id": "partner_node"},
    ]
    result = DHTNode._filter_providers_by_group(node, providers)
    assert len(result) == 2
    assert result[0]["provider_node_id"] == "partner_node"


# ── Initial Trust Tests ───────────────────────────────────────

def test_initial_trust_from_group():
    """Partner starts at 0.9, stranger at 0.3."""
    from unittest.mock import MagicMock
    node = MagicMock()
    node._config = {"reputation": {"initial_trust": {"partners": 0.9, "default": 0.3}}}
    engine = MagicMock()
    engine.get_groups.side_effect = lambda nid: ["partners"] if nid == "partner" else []
    node._group_engine = engine

    from knarr.dht.node import DHTNode
    assert DHTNode._get_initial_trust(node, "partner") == 0.9
    assert DHTNode._get_initial_trust(node, "stranger") == 0.3


# ── Storage Pull Methods ─────────────────────────────────────

def test_pull_outbox_query():
    """get_pending_outbox_for_requester filters by to_node."""
    from knarr.dht.storage import Storage
    import json
    storage = Storage(":memory:")
    node_a = "a" * 64
    node_b = "b" * 64
    storage.enqueue_outbox("i1", node_a, json.dumps({"item_id": "i1"}), 9999999999)
    storage.enqueue_outbox("i2", node_b, json.dumps({"item_id": "i2"}), 9999999999)
    storage.enqueue_outbox("i3", node_a, json.dumps({"item_id": "i3"}), 9999999999)

    items = storage.get_pending_outbox_for_requester(node_a, limit=10)
    assert len(items) == 2
    assert all(item["to_node"] == node_a for item in items)

    items = storage.get_pending_outbox_for_requester(node_b, limit=10)
    assert len(items) == 1
    assert items[0]["to_node"] == node_b

    storage.close()


def test_mark_outbox_pull_delivered():
    """mark_outbox_pull_delivered sets status and pull_delivered_at."""
    from knarr.dht.storage import Storage
    import json
    storage = Storage(":memory:")
    node_a = "a" * 64
    storage.enqueue_outbox("i1", node_a, json.dumps({"item_id": "i1"}), 9999999999)

    storage.mark_outbox_pull_delivered(["i1"], node_a)

    row = storage._get_conn().execute(
        "SELECT status, pull_delivered_at FROM mail_outbox WHERE item_id = 'i1'"
    ).fetchone()
    assert row[0] == "delivered"
    assert row[1] is not None
    storage.close()
