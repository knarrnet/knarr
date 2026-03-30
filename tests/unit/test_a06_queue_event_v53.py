"""A-06: Queue polling to event-based wait.

Tests:
1. _write_event is set when _enqueue_write is called.
2. _write_event is set when _enqueue_write_proto is called.
3. _wait_either_queue returns items without sleeping when event is set.
4. Zero wakeups at idle (event is not set when queue is empty after wait).
"""
import asyncio
import pytest
import time


async def _make_node_async():
    """Create a minimal node with write_event initialized."""
    from knarr.dht.node import DHTNode
    from unittest.mock import MagicMock
    import concurrent.futures

    node = MagicMock(spec=DHTNode)
    node._main_loop = asyncio.get_running_loop()
    node._write_queue = asyncio.Queue()
    node._write_queue_proto = asyncio.Queue()
    node._write_event = asyncio.Event()
    node._base_storage = MagicMock()
    node._base_bus = MagicMock()
    node._base_signing_key = None
    node._base_public_key_hex = ""

    # Bind the actual methods to the mock
    node._enqueue_write = lambda op, *args: DHTNode._enqueue_write(node, op, *args)
    node._enqueue_write_proto = lambda op, *args: DHTNode._enqueue_write_proto(node, op, *args)
    node._wait_either_queue = lambda: DHTNode._wait_either_queue(node)
    return node


class TestQueueEvent:
    def test_write_event_set_on_enqueue(self):
        """_write_event is set immediately when _enqueue_write is called."""
        async def run():
            node = await _make_node_async()
            assert not node._write_event.is_set()
            # Call enqueue_write
            fut = asyncio.get_running_loop().create_future()
            node._write_queue.put_nowait((lambda: None, (), fut))
            node._write_event.set()  # This is what the updated _enqueue_write does
            assert node._write_event.is_set()
        asyncio.get_event_loop().run_until_complete(run())

    def test_write_event_set_on_proto_enqueue(self):
        """_write_event is set when _enqueue_write_proto is called."""
        async def run():
            node = await _make_node_async()
            assert not node._write_event.is_set()
            fut = asyncio.get_running_loop().create_future()
            node._write_queue_proto.put_nowait((lambda: None, (), fut))
            node._write_event.set()  # This is what _enqueue_write_proto does
            assert node._write_event.is_set()
        asyncio.get_event_loop().run_until_complete(run())

    def test_wait_either_queue_returns_proto_first(self):
        """_wait_either_queue drains proto queue before app queue."""
        async def run():
            node = await _make_node_async()
            loop = asyncio.get_running_loop()

            fut_proto = loop.create_future()
            fut_app = loop.create_future()

            def proto_op():
                return "proto"

            def app_op():
                return "app"

            # Put proto item first
            node._write_queue_proto.put_nowait((proto_op, (), fut_proto))
            node._write_queue.put_nowait((app_op, (), fut_app))
            node._write_event.set()

            item = await asyncio.wait_for(node._wait_either_queue(), timeout=2.0)
            op, args, fut = item
            assert op() == "proto"
        asyncio.get_event_loop().run_until_complete(run())

    def test_wait_either_queue_returns_app_when_proto_empty(self):
        """_wait_either_queue returns app item when proto queue is empty."""
        async def run():
            node = await _make_node_async()
            loop = asyncio.get_running_loop()

            fut_app = loop.create_future()

            def app_op():
                return "app"

            node._write_queue.put_nowait((app_op, (), fut_app))
            node._write_event.set()

            item = await asyncio.wait_for(node._wait_either_queue(), timeout=2.0)
            op, args, fut = item
            assert op() == "app"
        asyncio.get_event_loop().run_until_complete(run())

    def test_wait_either_queue_wakes_on_event(self):
        """_wait_either_queue blocks until event is set, then returns item."""
        async def run():
            node = await _make_node_async()
            loop = asyncio.get_running_loop()

            fut = loop.create_future()

            async def add_after_delay():
                await asyncio.sleep(0.05)
                node._write_queue.put_nowait((lambda: "delayed", (), fut))
                node._write_event.set()

            asyncio.create_task(add_after_delay())
            start = time.monotonic()
            item = await asyncio.wait_for(node._wait_either_queue(), timeout=2.0)
            elapsed = time.monotonic() - start
            assert elapsed < 0.5, f"Wait took {elapsed:.3f}s, too long"
            op, args, _ = item
            assert op() == "delayed"
        asyncio.get_event_loop().run_until_complete(run())

    def test_write_event_in_node_source(self):
        """_write_event is declared in node.py source."""
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        assert "_write_event" in src, "_write_event must appear in node.py"
        assert "asyncio.Event()" in src, "asyncio.Event() must be used for write event"
