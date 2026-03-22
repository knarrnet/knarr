"""CR-04: settlement_confirmation wire body includes debt_component and target_balance_component.

BUG: After the hotfix that accepts overpayment, the payee has no way to validate the
exact expected settlement amount (debt + target_balance). The settlement_confirmation
body lacked debt_component and target_balance_component fields.

FIX: Add debt_component (actual debt) and target_balance_component (credit float) to
_build_settlement_confirmation_body(). Pass from _handle_settlement_request via
current_balance and amount from the inbound settle_request body.
"""
import pytest
from unittest.mock import MagicMock, patch


def _make_settlement_node():
    """Create a minimal DHTNode stub for testing settlement confirmation body building."""
    from knarr.dht.node import DHTNode
    from knarr.core.models import NodeInfo
    from nacl.signing import SigningKey

    node = MagicMock(spec=DHTNode)
    node.node_info = NodeInfo(node_id="aa" * 32, host="127.0.0.1", port=9000)
    node._public_key_hex = "bb" * 32
    signing_key = SigningKey.generate()
    node._signing_key = signing_key
    node._debug = False

    return node


@pytest.mark.asyncio
async def test_settlement_confirmation_body_has_debt_component():
    """_build_settlement_confirmation_body() must include debt_component field."""
    from knarr.dht.node import DHTNode

    node = _make_settlement_node()
    peer_key = "cc" * 32

    body = DHTNode._build_settlement_confirmation_body(
        node,
        peer_key=peer_key,
        amount=150.0,
        accepted_receipt_id="recv-001",
        settle_request_ref="req-001",
        debt_component=100.0,
        target_balance_component=50.0,
    )

    assert "debt_component" in body, (
        f"settlement_confirmation body missing debt_component. "
        f"CR-04: must include debt breakdown for payee validation. Body keys: {list(body.keys())}"
    )
    assert body["debt_component"] == 100.0, (
        f"Expected debt_component=100.0, got {body['debt_component']}"
    )


@pytest.mark.asyncio
async def test_settlement_confirmation_body_has_target_balance_component():
    """_build_settlement_confirmation_body() must include target_balance_component field."""
    from knarr.dht.node import DHTNode

    node = _make_settlement_node()
    peer_key = "cc" * 32

    body = DHTNode._build_settlement_confirmation_body(
        node,
        peer_key=peer_key,
        amount=150.0,
        accepted_receipt_id="recv-002",
        settle_request_ref="req-002",
        debt_component=100.0,
        target_balance_component=50.0,
    )

    assert "target_balance_component" in body, (
        f"settlement_confirmation body missing target_balance_component. "
        f"CR-04: must include target balance for payee validation. Body keys: {list(body.keys())}"
    )
    assert body["target_balance_component"] == 50.0, (
        f"Expected target_balance_component=50.0, got {body['target_balance_component']}"
    )


@pytest.mark.asyncio
async def test_settlement_confirmation_body_defaults_zero():
    """_build_settlement_confirmation_body() defaults to 0.0 for backwards compatibility."""
    from knarr.dht.node import DHTNode

    node = _make_settlement_node()
    peer_key = "dd" * 32

    # Call without the new optional parameters (backwards compat)
    body = DHTNode._build_settlement_confirmation_body(
        node,
        peer_key=peer_key,
        amount=75.0,
        accepted_receipt_id="recv-003",
        settle_request_ref="req-003",
    )

    assert "debt_component" in body, "debt_component must be present even when defaulted"
    assert "target_balance_component" in body, "target_balance_component must be present even when defaulted"
    assert body["debt_component"] == 0.0, f"Expected 0.0 default, got {body['debt_component']}"
    assert body["target_balance_component"] == 0.0, f"Expected 0.0 default, got {body['target_balance_component']}"


@pytest.mark.asyncio
async def test_settlement_confirmation_body_existing_fields_preserved():
    """Existing mandatory fields must still be present after CR-04 change."""
    from knarr.dht.node import DHTNode

    node = _make_settlement_node()
    peer_key = "ee" * 32

    body = DHTNode._build_settlement_confirmation_body(
        node,
        peer_key=peer_key,
        amount=200.0,
        accepted_receipt_id="recv-004",
        settle_request_ref="req-004",
        debt_component=200.0,
        target_balance_component=0.0,
    )

    mandatory_fields = [
        "type", "tx_hash", "amount_settled", "timestamp",
        "peer_key", "recipient", "counterparty_key",
        "accepted_receipt_id", "settle_request_ref",
    ]
    for field in mandatory_fields:
        assert field in body, (
            f"Mandatory field '{field}' missing from settlement_confirmation body after CR-04 change."
        )

    assert body["amount_settled"] == 200.0
    assert body["accepted_receipt_id"] == "recv-004"
    assert body["settle_request_ref"] == "req-004"
