import asyncio
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nacl.signing import SigningKey

from knarr.commerce.pricing_engine import DiscountRule, PricingConfig
from knarr.core.messages import MailSync
from knarr.core.models import NodeInfo, SkillSheet
from knarr.dht.storage import Storage
from knarr.mail.sync import SyncEngine


class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, event_type, **fields):
        self.events.append((event_type, fields))


class FakeGroupEngine:
    def __init__(self, memberships=None):
        self._memberships = memberships or {}

    def get_groups(self, node_id):
        return list(self._memberships.get(node_id, set()))


class FakeNode:
    def __init__(self, *, price=1.0, config=None, memberships=None, discount_rules=None):
        self.node_info = NodeInfo(node_id="f" * 64, host="127.0.0.1", port=9000)
        self.storage = Storage(":memory:")
        self.policy = SimpleNamespace(tit_for_tat=False)
        self.bus = FakeBus()
        self._group_engine = FakeGroupEngine(memberships)
        self._discount_rules = list(discount_rules or [])
        self._reminders = []
        self._config = {
            "mail": {"price": price},
            "economy": {"default_soft_limit": 1.0, "default_hard_limit": 0.0},
            "skills": {},
        }
        if config:
            for key, value in config.items():
                if isinstance(value, dict) and isinstance(self._config.get(key), dict):
                    merged = dict(self._config[key])
                    merged.update(value)
                    self._config[key] = merged
                else:
                    self._config[key] = value
        self._own_skills = {
            "knarr-mail": SkillSheet(
                "knarr-mail",
                "1.0.0",
                "mail",
                ["system"],
                {},
                {},
                price=price,
            )
        }

    async def _enqueue_write(self, op, *args):
        return op(*args)

    def _resolve_policy(self, public_key, skill_name):
        economy = self._config.get("economy", {})
        return float(economy.get("default_soft_limit", 3.0)), float(economy.get("default_hard_limit", -10.0))

    def _get_initial_trust(self, node_id):
        return 0.3

    def _load_discount_rules(self, peer_nid, skill_name):
        return list(self._discount_rules)

    def _get_cost_projection(self, skill_name):
        return None

    def _build_pricing_config(self, skill_name):
        pricing = self._config.get("pricing", {})
        return PricingConfig(
            discount_mode=pricing.get("discount_mode", "multiplicative"),
            discount_cap_pct=float(pricing.get("discount_cap_pct", 100.0)),
            markup_minimum=float(pricing.get("markup_minimum", 1.1)),
            min_price=float(pricing.get("min_price", 0.01)),
            global_min_price=float(pricing.get("global_min_price", 0.0)),
        )

    def _get_skill_runtime_config(self, skill_name):
        return dict(self._config.get("skills", {}).get(skill_name, {}))

    async def _maybe_send_tab_reminder(self, peer_public_key, balance, initial_credit, min_balance):
        self._reminders.append((peer_public_key, balance, initial_credit, min_balance))

    def _sign(self, msg):
        return msg

    def _write_receipt(self, *args, **kwargs):
        return None


def _sender():
    signing_key = SigningKey.generate()
    public_key = signing_key.verify_key.encode().hex()
    node_id = hashlib.sha256(signing_key.verify_key.encode()).hexdigest()
    return public_key, node_id


def _mail_sync(public_key, sender_node_id, item_id, msg_type="chat"):
    return MailSync(
        sender_node_id=sender_node_id,
        batch_seq=1,
        items=[{
            "item_id": item_id,
            "timestamp": time.time(),
            "ttl_expires": time.time() + 3600,
            "msg_type": msg_type,
            "body": {"text": item_id},
        }],
        public_key=public_key,
        signature="sig",
    )


@pytest.mark.asyncio
async def test_first_mail_from_unknown_sender_is_accepted():
    public_key, sender_node_id = _sender()
    node = FakeNode(price=1.0)
    engine = SyncEngine(node)

    ack = await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m1"), "127.0.0.1")

    assert ack.item_ids == ["m1"]
    assert node.storage.count_mail_inbox() == 1
    assert node.storage.get_ledger_balance(public_key) == 0.0


@pytest.mark.asyncio
async def test_subsequent_mail_drains_credit_and_eventually_rejects():
    public_key, sender_node_id = _sender()
    node = FakeNode(price=1.0)
    engine = SyncEngine(node)

    ack1 = await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m1"), "127.0.0.1")
    ack2 = await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m2"), "127.0.0.1")

    assert ack1.item_ids == ["m1"]
    assert ack2.item_ids == ["m2"]  # F6 fix: rejected items are ACKed to prevent infinite retry
    assert node.storage.count_mail_inbox() == 1


@pytest.mark.asyncio
async def test_system_mail_always_accepted_regardless_of_credit_state():
    public_key, sender_node_id = _sender()
    node = FakeNode(price=1.0)
    node.storage.get_or_create_ledger_entry(public_key, 0.0, 0.3)
    node.storage.update_ledger_provider(public_key, 999.0)
    engine = SyncEngine(node)
    engine.register_handler("knarr/system/test", AsyncMock())

    ack = await engine.handle_mail_sync(
        _mail_sync(public_key, sender_node_id, "sys1", msg_type="knarr/system/test"),
        "127.0.0.1",
    )
    await asyncio.sleep(0)

    assert ack.item_ids == ["sys1"]
    assert node.storage.get_mail_message("sys1", node.node_info.node_id) is not None


@pytest.mark.asyncio
async def test_felag_member_mail_is_free():
    public_key, sender_node_id = _sender()
    node = FakeNode(
        price=1.0,
        config={
            "economy": {"default_soft_limit": 0.0, "default_hard_limit": 0.0},
            "pricing": {"min_price": 0.0},
        },
        memberships={sender_node_id: {"felag"}},
        discount_rules=[
            DiscountRule(
                name="felag-free",
                group_name="felag",
                skill_group="*",
                effect_pct=100.0,
                priority=10,
            )
        ],
    )
    engine = SyncEngine(node)

    ack = await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m1"), "127.0.0.1")

    assert ack.item_ids == ["m1"]
    assert node.storage.get_ledger_balance(public_key) == 0.0


@pytest.mark.asyncio
async def test_credit_overdrawn_bus_event_emitted_on_rejection():
    public_key, sender_node_id = _sender()
    node = FakeNode(price=1.0)
    engine = SyncEngine(node)

    await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m1"), "127.0.0.1")
    await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m2"), "127.0.0.1")

    assert any(event_type == "credit.sanctioned" for event_type, _ in node.bus.events)


@pytest.mark.asyncio
async def test_forgiving_tab_allows_sender_to_mail_again():
    public_key, sender_node_id = _sender()
    node = FakeNode(price=1.0)
    engine = SyncEngine(node)

    await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m1"), "127.0.0.1")
    rejected = await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m2"), "127.0.0.1")
    node.storage.update_ledger_refund(public_key, 1.0)
    accepted = await engine.handle_mail_sync(_mail_sync(public_key, sender_node_id, "m3"), "127.0.0.1")

    assert rejected.item_ids == ["m2"]  # F6 fix: rejected items are ACKed to prevent infinite retry
    assert accepted.item_ids == ["m3"]
    assert node.storage.count_mail_inbox() == 2


@pytest.mark.asyncio
async def test_system_mail_bypasses_admission_entirely():
    public_key, sender_node_id = _sender()
    node = FakeNode(price=1.0)
    node.storage.get_or_create_ledger_entry = MagicMock(side_effect=AssertionError("ledger lookup should be skipped"))
    node._load_discount_rules = MagicMock(side_effect=AssertionError("pricing should be skipped"))
    engine = SyncEngine(node)
    engine.register_handler("knarr/system/test", AsyncMock())

    ack = await engine.handle_mail_sync(
        _mail_sync(public_key, sender_node_id, "sys1", msg_type="knarr/system/test"),
        "127.0.0.1",
    )
    await asyncio.sleep(0)

    assert ack.item_ids == ["sys1"]
