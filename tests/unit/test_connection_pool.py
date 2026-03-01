import asyncio
import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from knarr.dht.pool import ConnectionPool
from knarr.core.messages import Heartbeat


def _mock_stream():
    """Create a mock (reader, writer) pair that passes _is_healthy checks."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.at_eof = MagicMock(return_value=False)
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.is_closing = MagicMock(return_value=False)
    return reader, writer


@pytest.fixture
def mock_stream():
    reader, writer = _mock_stream()
    with patch("knarr.dht.pool.receive_message", return_value=Heartbeat(node_id="peer")):
        yield reader, writer

@pytest.mark.asyncio
async def test_pool_reuses_connection():
    """Second send to same peer does not open new TCP connection."""
    pool = ConnectionPool()
    peer_id = "peer1"
    host, port = "127.0.0.1", 9000
    msg = Heartbeat(node_id="self")

    mock_reader, mock_writer = _mock_stream()

    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)) as mock_open:
        with patch("knarr.dht.pool.receive_message", return_value=Heartbeat(node_id="peer1")):
            # First send
            await pool.send(peer_id, host, port, msg)
            assert mock_open.call_count == 1

            # Second send
            await pool.send(peer_id, host, port, msg)
            assert mock_open.call_count == 1

@pytest.mark.asyncio
async def test_pool_evicts_lifo():
    """When pool is full, newest idle connection is evicted (not oldest)."""
    pool = ConnectionPool(max_connections=2)

    mock_reader, mock_writer = _mock_stream()

    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
        with patch("knarr.dht.pool.receive_message", return_value=Heartbeat(node_id="any")):
            # Fill pool
            await pool.send("peer_a", "1.1.1.1", 9000, Heartbeat())
            await pool.send("peer_b", "2.2.2.2", 9000, Heartbeat())
            assert pool.size == 2

            # Trigger eviction by adding peer_c.
            # creation_order is ["peer_a", "peer_b"].
            # LIFO should evict peer_b (most recently added).
            await pool.send("peer_c", "3.3.3.3", 9000, Heartbeat())
            assert pool.size == 2
            assert "peer_b" not in pool._pool
            assert "peer_a" in pool._pool
            assert "peer_c" in pool._pool

@pytest.mark.asyncio
async def test_pool_removes_broken_connection():
    """Broken pipe removes connection, next send reconnects."""
    pool = ConnectionPool()
    peer_id = "peer1"

    call_count = {"open": 0}

    async def tracked_open(*args, **kwargs):
        call_count["open"] += 1
        return _mock_stream()

    with patch("asyncio.open_connection", side_effect=tracked_open):
        with patch("knarr.dht.pool.send_message") as mock_send:
            with patch("knarr.dht.pool.receive_message") as mock_recv:
                # First send: works fine
                mock_recv.return_value = Heartbeat(node_id="peer1")
                result = await pool.send(peer_id, "1.1.1.1", 9000, Heartbeat())
                assert pool.size == 1
                assert call_count["open"] == 1

                # Second send: receive raises, triggering reconnect + retry
                mock_recv.side_effect = [ConnectionError("broken"), Heartbeat(node_id="peer1")]
                result = await pool.send(peer_id, "1.1.1.1", 9000, Heartbeat())
                # Should have opened a new connection (reconnect after broken)
                assert call_count["open"] >= 2
                # Pool should still have the peer (reconnected)
                assert pool.size == 1

@pytest.mark.asyncio
async def test_pool_idle_eviction():
    """Connections unused past timeout are closed."""
    pool = ConnectionPool()
    r, w = _mock_stream()
    pool._pool["peer1"] = (r, w)
    pool._last_used["peer1"] = time.monotonic() - 100

    await pool.evict_idle(idle_timeout=50)
    assert pool.size == 0

@pytest.mark.asyncio
async def test_pool_close_all():
    """Shutdown closes all connections."""
    pool = ConnectionPool()
    pool._pool["p1"] = _mock_stream()
    pool._pool["p2"] = _mock_stream()

    await pool.close_all()
    assert pool.size == 0

@pytest.mark.asyncio
async def test_pool_lock_serializes_sends():  # SENTINEL
    """Concurrent sends on same connection are serialized by lock."""
    pool = ConnectionPool()
    peer_id = "peer1"

    mock_reader, mock_writer = _mock_stream()

    call_order = []

    async def slow_send(writer, msg):
        call_order.append(f"start_{msg.node_id}")
        await asyncio.sleep(0.1)
        call_order.append(f"end_{msg.node_id}")

    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
        with patch("knarr.dht.pool.send_message", side_effect=slow_send):
            with patch("knarr.dht.pool.receive_message", return_value=Heartbeat()):
                await asyncio.gather(
                    pool.send(peer_id, "1.1.1.1", 9000, Heartbeat(node_id="1")),
                    pool.send(peer_id, "1.1.1.1", 9000, Heartbeat(node_id="2"))
                )

    # Verify no interleaving: start_1, end_1, start_2, end_2 (or 2 then 1)
    assert len(call_order) == 4
    assert call_order[0].startswith("start")
    assert call_order[1].startswith("end")
    assert call_order[2].startswith("start")
    assert call_order[3].startswith("end")
    assert call_order[0][6:] == call_order[1][4:]
    assert call_order[2][6:] == call_order[3][4:]

@pytest.mark.asyncio
async def test_pool_concurrent_first_send():
    """Two concurrent sends to a NEW peer only open 1 connection."""
    pool = ConnectionPool()
    peer_id = "new_peer"

    call_count = {"open": 0}
    mock_reader, mock_writer = _mock_stream()

    async def tracked_open(*args, **kwargs):
        call_count["open"] += 1
        await asyncio.sleep(0.05)  # simulate connection latency
        return mock_reader, mock_writer

    with patch("asyncio.open_connection", side_effect=tracked_open):
        with patch("knarr.dht.pool.send_message"):
            with patch("knarr.dht.pool.receive_message", return_value=Heartbeat(node_id=peer_id)):
                await asyncio.gather(
                    pool.send(peer_id, "1.1.1.1", 9000, Heartbeat(node_id="a")),
                    pool.send(peer_id, "1.1.1.1", 9000, Heartbeat(node_id="b"))
                )

    # Lock serializes: first sender creates connection, second reuses it
    assert call_count["open"] == 1
    assert pool.size == 1
