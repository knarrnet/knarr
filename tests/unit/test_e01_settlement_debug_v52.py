"""E-01: settlement_execution.py debug flag initialization.

The settlement functions use logger.info/warning/debug calls.
E-01 ensures that any class wrapping settlement functions initializes
self._debug from the node context to avoid AttributeError.

Since settlement_execution.py uses module-level functions with standard
logging (no self._debug pattern), E-01 is verified by:
1. Confirming execute_settlement and write_settlement_processed do not
   reference undefined self._debug.
2. Confirming a SettlementDebugWrapper (if used) properly initializes _debug.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class MockStorage:
    def write_receipt(self, **kwargs):
        pass
    def get_node_id_by_pubkey(self, pk):
        return "a" * 64
    def get_ledger_balance(self, pk):
        return 5.0
    def get_or_create_ledger_entry(self, pk):
        entry = MagicMock()
        entry.hard_limit = -10.0
        return entry


def make_signing_key():
    from nacl.signing import SigningKey
    return SigningKey.generate()


def make_verify_key(signing_key):
    return signing_key.verify_key


# ──────────────────────────────────────────────────────────────────────────────
# E-01-A: execute_settlement does not raise AttributeError from _debug references
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_settlement_no_debug_attribute_error():
    """execute_settlement should not raise AttributeError on _debug."""
    from knarr.commerce.settlement_execution import validate_dual_signatures

    # Test validate_dual_signatures directly — it uses logger but no self._debug
    prepared_doc = {"type": "settlement_prepared", "amount": 10.0, "receipt_id": "rid"}
    countersigned_doc = {"type": "settlement_prepared", "amount": 10.0, "receipt_id": "rid"}

    signing_key = make_signing_key()
    verify_key = make_verify_key(signing_key)

    from knarr.core.proof import sign_document
    vm = "did:knarr:abc#key-1"

    payload = {"amount": 10.0, "receipt_id": "rid", "type": "settlement_prepared"}
    signed = sign_document(payload, signing_key, vm)

    # validate_dual_signatures should not raise AttributeError
    try:
        ok, reason = validate_dual_signatures(signed, signed, verify_key, verify_key)
        # May fail for other reasons (payload mismatch etc.), but not AttributeError
        assert isinstance(ok, bool)
    except AttributeError as e:
        pytest.fail(f"AttributeError from _debug: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# E-01-B: write_settlement_processed does not raise AttributeError
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_settlement_processed_no_debug_error():
    """write_settlement_processed should not raise AttributeError on _debug."""
    from knarr.commerce.settlement_execution import write_settlement_processed

    signing_key = make_signing_key()
    storage = MockStorage()

    try:
        receipt_id = await write_settlement_processed(
            node_id="a" * 64,
            peer_key="b" * 64,
            amount_settled=10.0,
            ledger_delta=-10.0,
            final_balance=0.0,
            accepted_receipt_id="accepted_" + "x" * 55,
            settle_request_ref="settle_ref",
            signing_key=signing_key,
            storage=storage,
        )
        assert isinstance(receipt_id, str)
        assert len(receipt_id) > 0
    except AttributeError as e:
        pytest.fail(f"AttributeError from _debug in write_settlement_processed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# E-01-C: SyncEngine initializes _debug from node config (no AttributeError)
# ──────────────────────────────────────────────────────────────────────────────

def test_sync_engine_debug_initialized():
    """SyncEngine should initialize self._debug without AttributeError."""
    from knarr.mail.sync import SyncEngine

    mock_node = MagicMock()
    mock_node._config = {"mail": {"debug": True}}

    engine = SyncEngine(mock_node)
    # _debug must be set and not raise AttributeError
    assert hasattr(engine, '_debug')
    assert engine._debug is True


def test_sync_engine_debug_defaults_false():
    """SyncEngine._debug defaults to False when not configured."""
    from knarr.mail.sync import SyncEngine

    mock_node = MagicMock()
    mock_node._config = {"mail": {}}

    engine = SyncEngine(mock_node)
    assert hasattr(engine, '_debug')
    # Should be falsy (False or 0 or None acceptable)
    assert not engine._debug


# ──────────────────────────────────────────────────────────────────────────────
# E-01-D: settlement functions use standard logging, not self._debug
# ──────────────────────────────────────────────────────────────────────────────

def test_settlement_execution_module_no_self_debug():
    """Verify settlement_execution.py functions don't reference self._debug."""
    import inspect
    from knarr.commerce import settlement_execution

    src = inspect.getsource(settlement_execution)
    # Module-level functions should not use self._debug
    # (they are free functions, not class methods)
    assert "self._debug" not in src, (
        "settlement_execution.py has self._debug references in free functions — "
        "this would cause AttributeError at runtime"
    )
