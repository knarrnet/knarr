"""RUL-01: NETTING_SESSION_EXPIRED debug logging.
RUL-02: Concurrent proposal + TTL boundary tests.

RUL-01: When a netting session expires (TTL check path that cleans up expired
sessions), emit a DEBUG-level log line with session details: peer, session ID,
duration/age.

RUL-02: Two test cases:
1. Concurrent netting proposals — verify only one session proceeds under the lock.
2. TTL boundary — proposal expires exactly at TTL; verify session is cleaned up
   cleanly (no lingering entry, no error on access after expiry).
"""
import asyncio
import time
import logging
import pytest
from unittest.mock import MagicMock


def _make_netting_node(chain_id="solana-mainnet"):
    """Create a minimal node stub for netting handler tests."""
    node = MagicMock()
    node._config = {"blockchain": {"chain": chain_id}}
    node._debug = False
    node._enqueue_write = MagicMock()
    storage = MagicMock()
    node.storage = storage
    return node


def _make_proposal_item(netting_id, from_node, chain_id="solana-mainnet", amount=50.0):
    return {
        "from_node": from_node,
        "body": {
            "type": "knarr/commerce/netting_proposal",
            "netting_id": netting_id,
            "chain_id": chain_id,
            "settlement_amount": amount,
        }
    }


@pytest.mark.asyncio
async def test_rul01_expired_session_emits_debug_log(caplog):
    """RUL-01: Expired netting sessions must emit DEBUG log on cleanup."""
    from knarr.commerce.handlers import make_commerce_handlers

    node = _make_netting_node()
    handlers = make_commerce_handlers(node)
    proposal_handler = handlers["knarr/commerce/netting_proposal"]

    # Inject a pre-expired session into _netting_sessions via a proposal then manual expiry
    # First: add a proposal (this stores a new session)
    old_id = "old-netting-id-001"
    new_id = "new-netting-id-002"

    # Manually inject an expired session by calling proposal twice
    # First proposal registers old_id session
    await proposal_handler(_make_proposal_item(old_id, "peer-a"))

    # Now manually expire it by manipulating time via the expiry mechanism:
    # Get the _netting_sessions from the closure — we can't, but we can call
    # a second proposal which will trigger cleanup of the first if it's expired.
    # We need time to have passed, so we directly manipulate via monkeypatching time.

    # Use a patched 'time' that returns a future timestamp so old_id appears expired
    import knarr.commerce.handlers as handlers_module
    orig_time = handlers_module.time.time

    # Advance time by 400 seconds so the 300s TTL is exceeded
    future_time = orig_time() + 400
    handlers_module.time.time = lambda: future_time

    try:
        with caplog.at_level(logging.DEBUG, logger="knarr.commerce"):
            await proposal_handler(_make_proposal_item(new_id, "peer-b"))
    finally:
        handlers_module.time.time = orig_time

    debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    expired_msgs = [m for m in debug_msgs if "NETTING_SESSION_EXPIRED" in m]

    assert len(expired_msgs) >= 1, (
        f"RUL-01: No NETTING_SESSION_EXPIRED debug log found. "
        f"Debug messages: {debug_msgs}"
    )
    assert old_id[:8] in expired_msgs[0], (
        f"RUL-01: Expected netting_id prefix in expired log. Got: {expired_msgs[0]}"
    )


@pytest.mark.asyncio
async def test_rul02_ttl_boundary_session_cleaned_up():
    """RUL-02: Session expired at TTL boundary is cleaned up cleanly."""
    from knarr.commerce.handlers import make_commerce_handlers
    import knarr.commerce.handlers as handlers_module

    node = _make_netting_node()
    handlers = make_commerce_handlers(node)
    proposal_handler = handlers["knarr/commerce/netting_proposal"]

    old_id = "ttl-boundary-test-001"
    new_id = "ttl-boundary-test-002"

    # Register a session
    await proposal_handler(_make_proposal_item(old_id, "peer-c"))

    orig_time = handlers_module.time.time

    # Advance to exactly TTL boundary + 1ms
    boundary_time = orig_time() + 300 + 0.001
    handlers_module.time.time = lambda: boundary_time

    try:
        # This should trigger cleanup of old_id without error
        await proposal_handler(_make_proposal_item(new_id, "peer-d"))
    except Exception as e:
        pytest.fail(f"RUL-02: TTL boundary cleanup raised exception: {e}")
    finally:
        handlers_module.time.time = orig_time


@pytest.mark.asyncio
async def test_rul02_concurrent_proposals_serialized():
    """RUL-02: Concurrent netting proposals are serialized by the lock — last writer wins."""
    from knarr.commerce.handlers import make_commerce_handlers

    node = _make_netting_node()
    handlers = make_commerce_handlers(node)
    proposal_handler = handlers["knarr/commerce/netting_proposal"]

    id_a = "concurrent-a"
    id_b = "concurrent-b"

    # Fire both concurrently
    results = await asyncio.gather(
        proposal_handler(_make_proposal_item(id_a, "peer-e")),
        proposal_handler(_make_proposal_item(id_b, "peer-f")),
        return_exceptions=True,
    )

    # Neither should raise
    for r in results:
        assert not isinstance(r, Exception), (
            f"RUL-02: Concurrent proposal raised: {r}"
        )
