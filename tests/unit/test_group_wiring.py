"""Sentinel tests for GroupEngine policy consumer wiring.

IMPORTANT: These tests verify that the GroupEngine is correctly wired into
each policy consumer subsystem. Do NOT modify these tests to fix failures.
If a sentinel test fails, fix the integration code.
"""
import hashlib
import pytest
from knarr.core.groups import DefaultGroupEngine
from knarr.core.models import Policy, GroupPolicy
from knarr.dht.node import DHTNode

# Realistic identity: public_key (Ed25519 hex) and derived node_id
_PK_A = "aa" * 32  # 64-char hex public key
_NID_A = hashlib.sha256(bytes.fromhex(_PK_A)).hexdigest()  # node_id = SHA-256(pk)
_PK_B = "bb" * 32
_NID_B = hashlib.sha256(bytes.fromhex(_PK_B)).hexdigest()


def _make_node(**config_overrides) -> DHTNode:
    """Create a minimal DHTNode for wiring tests."""
    config = {
        "policy": {"initial_credit": 3.0, "min_balance": -10.0},
        "node": {"sidecar_port": 0},
    }
    config.update(config_overrides)
    node = DHTNode("127.0.0.1", 0, config=config)
    return node


class TestCreditLimitWiring:
    """ST-1: Credit limits resolve via GroupEngine + [credit.group_limits].

    Groups store node_ids (SHA-256 of public_key).
    _resolve_policy receives public_key and must convert before lookup.
    """

    def test_new_format_group_credit(self):
        """Peer in group gets credit from [credit.group_limits]."""
        node = _make_node(
            groups={"partners": {"type": "explicit", "members": [_NID_A]}},
            credit={"group_limits": {"partners": {"initial_credit": 500, "min_balance": -100}}},
        )
        node._init_group_engine()
        # Pass public_key — _resolve_policy must convert to node_id internally
        ic, mb = node._resolve_policy(_PK_A, "some_skill")
        assert ic == 500
        assert mb == -100

    def test_unknown_peer_gets_default(self):
        """Peer not in any group gets default credit."""
        node = _make_node(
            groups={"partners": {"type": "explicit", "members": [_NID_A]}},
            credit={"group_limits": {"partners": {"initial_credit": 500, "min_balance": -100}}},
        )
        node._init_group_engine()
        ic, mb = node._resolve_policy(_PK_B, "some_skill")
        assert ic == node.policy.initial_credit
        assert mb == node.policy.min_balance

    def test_highest_credit_wins(self):
        """Peer in multiple groups gets the highest initial_credit."""
        node = _make_node(
            groups={
                "partners": {"type": "explicit", "members": [_NID_A]},
                "vip": {"type": "explicit", "members": [_NID_A]},
            },
            credit={"group_limits": {
                "partners": {"initial_credit": 500, "min_balance": -100},
                "vip": {"initial_credit": 1000, "min_balance": -200},
            }},
        )
        node._init_group_engine()
        ic, mb = node._resolve_policy(_PK_A, "some_skill")
        assert ic == 1000
        assert mb == -200

    def test_public_key_without_conversion_fails(self):
        """SENTINEL: using public_key directly as group member must NOT match.

        This test guards against the v0.22.0 identity domain bug where
        _resolve_policy passed raw public_key to get_groups(), but groups
        store node_ids (SHA-256 of public_key).
        """
        node = _make_node(
            # Group member is the raw public_key (WRONG — should be node_id)
            groups={"bad": {"type": "explicit", "members": [_PK_A]}},
            credit={"group_limits": {"bad": {"initial_credit": 999, "min_balance": -999}}},
        )
        node._init_group_engine()
        # _resolve_policy converts PK→NID, so "bad" group with PK member won't match
        ic, mb = node._resolve_policy(_PK_A, "some_skill")
        assert ic == node.policy.initial_credit  # default, NOT 999


class TestFirewallBlocklistWiring:
    """ST-2: Firewall blocklist uses GroupEngine.is_member()."""

    def test_blocked_member(self):
        """is_member correctly identifies blocked nodes."""
        engine = DefaultGroupEngine({"blocked": {"bad_node_123"}})
        assert engine.is_member("bad_node_123", "blocked") is True

    def test_not_blocked(self):
        """is_member returns False for non-blocked nodes."""
        engine = DefaultGroupEngine({"blocked": {"bad_node_123"}})
        assert engine.is_member("good_node_456", "blocked") is False

    def test_empty_blocklist(self):
        """Empty blocked group blocks nobody."""
        engine = DefaultGroupEngine({"blocked": set()})
        assert engine.is_member("any_node", "blocked") is False


class TestMailAcceptWiring:
    """ST-3: Mail accept_from='groups' uses GroupEngine."""

    def test_group_engine_resolves_for_mail(self):
        """GroupEngine correctly resolves membership for mail accept decisions."""
        engine = DefaultGroupEngine({
            "mail_allowed": {"node_a", "node_b"},
            "partners": {"node_a"},
        })
        # node_a is in both groups
        groups_a = set(engine.get_groups("node_a"))
        accept_groups = {"mail_allowed", "partners"}
        assert groups_a.intersection(accept_groups)  # allowed

        # node_c is in neither group
        groups_c = set(engine.get_groups("node_c"))
        assert not groups_c.intersection(accept_groups)  # rejected


class TestBackwardCompat:
    """ST-4: Old [policy.group.X] config format still resolves credit limits.

    IMPORTANT: This test uses ONLY old-format config. No [groups.X] section.
    Old-format groups also store node_ids as members.
    """

    def test_old_format_credit_limits(self):
        """[policy.group.X] with initial_credit and min_balance still works."""
        node = _make_node()
        node._group_policies = [
            GroupPolicy(
                name="friends",
                members={_NID_A},
                members_file=None,
                initial_credit=300,
                min_balance=-50,
            )
        ]
        node._init_group_engine()
        ic, mb = node._resolve_policy(_PK_A, "any_skill")
        assert ic == 300
        assert mb == -50

    def test_old_format_engine_membership(self):
        """Old [policy.group.X] members are visible via GroupEngine."""
        node = _make_node(
            policy={"group": {"friends": {"members": [_NID_A]}},
                    "initial_credit": 3.0, "min_balance": -10.0}
        )
        node._init_group_engine()
        assert node._group_engine.is_member(_NID_A, "friends") is True
        assert "friends" in node._group_engine.get_groups(_NID_A)


class TestPluginOverride:
    """ST-5: Groups plugin replaces DefaultGroupEngine."""

    def test_default_engine_type(self):
        """Node starts with DefaultGroupEngine before plugins load."""
        node = _make_node(groups={"test": {"type": "explicit", "members": ["a"]}})
        node._init_group_engine()
        assert type(node._group_engine).__name__ == "DefaultGroupEngine"
        assert node._group_engine.is_member("a", "test") is True
