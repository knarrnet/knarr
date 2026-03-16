"""Tests for BUS-01: EventNotify dataclass + receive handler in node.py dispatch."""
import asyncio
import hashlib
import json
import unittest
from unittest.mock import MagicMock


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())


class TestEventNotifyDataclass(unittest.TestCase):
    def test_event_notify_is_message(self):
        from knarr.core.messages import EventNotify, Message

        self.assertTrue(issubclass(EventNotify, Message))

    def test_event_notify_default_type(self):
        from knarr.core.messages import EventNotify

        self.assertEqual(EventNotify().type, "EVENT_NOTIFY")

    def test_event_notify_serializable(self):
        from knarr.core.messages import EventNotify

        payload = EventNotify(event_type="credit.change", event_payload="{}").to_dict()
        self.assertEqual(payload["type"], "EVENT_NOTIFY")
        self.assertEqual(payload["event_type"], "credit.change")

    def test_hops_is_signature_excluded_for_relay_compat(self):
        """hops stays in SIGNATURE_EXCLUDED_FIELDS for Announce relay compatibility.

        TP-7 fix targets the hops>0 gate in _handle_event_notify, not the global excluded set.
        """
        from knarr.core.messages import SIGNATURE_EXCLUDED_FIELDS

        self.assertIn("hops", SIGNATURE_EXCLUDED_FIELDS)

    def test_deserialize_event_notify(self):
        from knarr.core.messages import EventNotify, deserialize_message

        payload = json.dumps({
            "type": "EVENT_NOTIFY",
            "msg_id": "test-id",
            "origin_node_id": "node123",
            "event_type": "mail.flush_skip",
            "event_payload": '{"to_node": "abc"}',
            "event_ts": 1000.0,
            "hops": 0,
        }).encode("utf-8")

        msg = deserialize_message(payload)
        self.assertIsInstance(msg, EventNotify)
        self.assertEqual(msg.event_type, "mail.flush_skip")


class TestEventBroadcastTopics(unittest.TestCase):
    def test_broadcast_topics_content(self):
        """TP-6 adversarial fix: credit.change removed (bilateral data leak)."""
        from knarr.dht.node import _EVENT_BROADCAST_TOPICS

        self.assertEqual(_EVENT_BROADCAST_TOPICS, frozenset({"mail.flush_skip"}))


class TestEventNotifyHandler(unittest.TestCase):
    def _make_signed_event_notify(self, event_type="credit.change", payload=None, hops=0):
        from nacl.signing import SigningKey
        from knarr.core.messages import EventNotify, sign_message

        if payload is None:
            payload = {"amount": 5.0}

        signing_key = SigningKey.generate()
        public_key = signing_key.verify_key.encode().hex()
        origin_node_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
        msg = EventNotify(
            origin_node_id=origin_node_id,
            event_type=event_type,
            event_payload=json.dumps(payload),
            event_ts=1234567890.0,
            hops=hops,
            public_key=public_key,
        )
        return sign_message(msg, signing_key)

    def _make_node(self):
        from knarr.dht.node import DHTNode

        node = DHTNode.__new__(DHTNode)
        node.bus = MagicMock()
        return node

    def test_valid_event_notify_emits_on_local_bus(self):
        """TP-6: credit.change removed from broadcast topics. Use mail.flush_skip."""
        from knarr.dht.node import DHTNode

        node = self._make_node()
        msg = self._make_signed_event_notify(
            event_type="mail.flush_skip",
            payload={
                "reason": "no_route",
                "event": "mail.flush_skip",   # stripped by handler
                "event_id": "strip-me",        # stripped by handler
                "ts": 10,                      # stripped by handler
                "valid_from": 1,               # stripped by handler
            }
        )

        result = _run_async(DHTNode._process_message(node, msg))

        self.assertIsNone(result)
        call_kwargs = node.bus.emit.call_args.kwargs if node.bus.emit.called else {}
        self.assertTrue(node.bus.emit.called, "bus.emit was not called")
        self.assertEqual(node.bus.emit.call_args.args[0], "mail.flush_skip")
        self.assertEqual(call_kwargs.get("reason"), "no_route")

    def test_hops_gt_zero_is_silently_dropped(self):
        from knarr.dht.node import DHTNode

        node = self._make_node()
        msg = self._make_signed_event_notify(hops=1)

        result = _run_async(DHTNode._process_message(node, msg))

        self.assertIsNone(result)
        node.bus.emit.assert_not_called()

    def test_disallowed_topic_is_dropped(self):
        from knarr.dht.node import DHTNode

        node = self._make_node()
        msg = self._make_signed_event_notify(event_type="peer.joined")

        result = _run_async(DHTNode._process_message(node, msg))

        self.assertIsNone(result)
        node.bus.emit.assert_not_called()

    def test_invalid_json_payload_is_dropped(self):
        from nacl.signing import SigningKey
        from knarr.core.messages import EventNotify, sign_message
        from knarr.dht.node import DHTNode

        node = self._make_node()
        signing_key = SigningKey.generate()
        public_key = signing_key.verify_key.encode().hex()
        origin_node_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
        msg = EventNotify(
            origin_node_id=origin_node_id,
            event_type="credit.change",
            event_payload="NOT VALID JSON {{{",
            hops=0,
            public_key=public_key,
        )
        msg = sign_message(msg, signing_key)

        result = _run_async(DHTNode._process_message(node, msg))

        self.assertIsNone(result)
        node.bus.emit.assert_not_called()

    def test_unsigned_event_notify_is_dropped(self):
        from knarr.core.messages import EventNotify
        from knarr.dht.node import DHTNode

        node = self._make_node()
        msg = EventNotify(
            origin_node_id="fake",
            event_type="credit.change",
            event_payload='{"amount": 1.0}',
            hops=0,
        )

        result = _run_async(DHTNode._process_message(node, msg))

        self.assertIsNone(result)
        node.bus.emit.assert_not_called()

    def test_mail_flush_skip_topic_is_accepted(self):
        from knarr.dht.node import DHTNode

        node = self._make_node()
        msg = self._make_signed_event_notify(
            event_type="mail.flush_skip",
            payload={"to_node": "abc", "reason": "no_route"},
        )

        result = _run_async(DHTNode._process_message(node, msg))

        self.assertIsNone(result)
        # bus.emit always receives origin_node=... kwarg alongside payload fields
        self.assertTrue(node.bus.emit.called, "bus.emit was not called for mail.flush_skip")
        self.assertEqual(node.bus.emit.call_args.args[0], "mail.flush_skip")
        call_kwargs = node.bus.emit.call_args.kwargs
        self.assertEqual(call_kwargs.get("to_node"), "abc")
        self.assertEqual(call_kwargs.get("reason"), "no_route")


if __name__ == "__main__":
    unittest.main()
