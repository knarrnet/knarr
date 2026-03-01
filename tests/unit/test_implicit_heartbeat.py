import asyncio
import hashlib
import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from knarr.dht.node import DHTNode, HEARTBEAT_CHECK_INTERVAL
from knarr.core.messages import Announce, Heartbeat, NodeInfo

# SA-ML6: signer_id is now derived from public_key, not node_id
_TEST_PUBKEY = "aa" * 32  # Valid 64-char hex
_TEST_SIGNER_ID = hashlib.sha256(bytes.fromhex(_TEST_PUBKEY)).hexdigest()

@pytest.mark.asyncio
async def test_any_message_updates_last_activity():
    """Receiving any message type updates peer's last_activity."""
    node = DHTNode("127.0.0.1", 0)
    # Mock stream for _handle_connection
    mock_reader = AsyncMock()
    mock_writer = MagicMock() # Use MagicMock for get_extra_info
    mock_writer.get_extra_info.return_value = ("1.1.1.1", 1234)
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    # Message from peer_a (SA-ML6: signer_id derived from public_key)
    msg = Announce(
        node_id="peer_a",
        skill_key="test",
        skill_sheet={"name": "test", "version": "1.0.0", "description": "test", "tags": ["test"], "input_schema": {}, "output_schema": {}},
        public_key=_TEST_PUBKEY,
        signature="def"
    )

    # Return message once, then None (EOF) to exit message loop
    with patch("knarr.dht.node.receive_message", side_effect=[msg, None]):
        with patch("knarr.dht.node.verify_message", return_value=True):
            with patch("knarr.dht.node.verify_node_id", return_value=True):
                # We need to bypass the process_message logic or mock it
                with patch.object(node, "_process_message", return_value=None):
                    # Mock _enqueue_write (writer task not started in unit tests)
                    node._enqueue_write = AsyncMock()
                    # We need to bypass the connection tracking
                    node._active_connections = 0
                    await node._handle_connection(mock_reader, mock_writer)

    assert _TEST_SIGNER_ID in node._peer_last_activity
    assert node._peer_last_activity[_TEST_SIGNER_ID] > 0

@pytest.mark.asyncio
async def test_silent_peer_gets_heartbeat():
    """Peer with no activity for >90s receives dedicated heartbeat."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    node._pool = AsyncMock()
    node._enqueue_write = AsyncMock()
    
    peer = NodeInfo(node_id="peer_s", host="1.1.1.1", port=9000)
    node.storage.get_peers = MagicMock(return_value=[peer])
    
    # Silence for 100s
    node._peer_last_activity["peer_s"] = time.monotonic() - 100
    node._heartbeat_silence_threshold = 90
    
    # Run one iteration of the loop logic (refactored)
    # We'll just call the logic block if we can, or mock sleep and run loop once.
    # For unit test, we can just trigger the loop once.
    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
        try:
            await node._heartbeat_loop()
        except asyncio.CancelledError:
            pass
            
    # Verify pool.send called with Heartbeat
    node._pool.send.assert_called()
    args, kwargs = node._pool.send.call_args
    assert args[0] == "peer_s"
    assert isinstance(args[3], Heartbeat)

@pytest.mark.asyncio
async def test_active_peer_skips_heartbeat():
    """Peer that sent message 30s ago gets no heartbeat."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    node._pool = AsyncMock()
    node._enqueue_write = AsyncMock()
    
    peer = NodeInfo(node_id="peer_a", host="1.1.1.1", port=9000)
    node.storage.get_peers = MagicMock(return_value=[peer])
    
    # Silence for 30s
    node._peer_last_activity["peer_a"] = time.monotonic() - 30
    node._heartbeat_silence_threshold = 90
    
    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
        try:
            await node._heartbeat_loop()
        except asyncio.CancelledError:
            pass
            
    # Verify pool.send NOT called
    node._pool.send.assert_not_called()

@pytest.mark.asyncio
async def test_dead_peer_removed_after_timeout():  # SENTINEL
    """Peer with >300s silence is removed from peer storage."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    node._pool = AsyncMock()
    node._enqueue_write = AsyncMock()
    
    peer = NodeInfo(node_id="peer_d", host="1.1.1.1", port=9000)
    node.storage.get_peers = MagicMock(return_value=[peer])
    
    # Silence for 400s
    node._peer_last_activity["peer_d"] = time.monotonic() - 400
    node._peer_dead_timeout = 300
    
    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
        try:
            await node._heartbeat_loop()
        except asyncio.CancelledError:
            pass
            
    # Verify storage.remove_peer called
    node._enqueue_write.assert_any_call(node.storage.remove_peer, "peer_d")
    assert "peer_d" not in node._peer_last_activity

@pytest.mark.asyncio
async def test_rebootstrap_when_no_peers():
    """Node with 0 peers re-bootstraps automatically."""
    node = DHTNode("127.0.0.1", 0)
    node._running = True
    node._pool = AsyncMock()
    node._enqueue_write = AsyncMock()
    node._bootstrap_peers = ["1.1.1.1:9000"]

    node.storage.get_peers = MagicMock(return_value=[])

    with patch.object(node, "join", new_callable=AsyncMock) as mock_join:
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            try:
                await node._heartbeat_loop()
            except asyncio.CancelledError:
                pass

    mock_join.assert_called_once_with(["1.1.1.1:9000"])
