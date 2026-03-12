"""Tests for settlement_execution.py — B2 dual-signature flow."""

import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock

from nacl.signing import SigningKey, VerifyKey

from knarr.commerce.settlement_execution import (
    prepare_settlement,
    validate_dual_signatures,
    execute_settlement,
    write_settlement_processed,
)
from knarr.core.proof import sign_document, verify_document


NODE_ID = "a" * 64
PEER_KEY = "b" * 64


def _make_signing_key():
    return SigningKey.generate()


def _make_storage():
    storage = MagicMock()
    storage.write_receipt = MagicMock()
    return storage


def _make_bus():
    bus = MagicMock()
    bus.emit = MagicMock()
    return bus


class TestPreparSettlement:
    def test_prepare_returns_signed_dict(self):
        sk = _make_signing_key()
        storage = _make_storage()
        result = asyncio.get_event_loop().run_until_complete(
            prepare_settlement(
                node_id=NODE_ID,
                peer_key=PEER_KEY,
                amount=50.0,
                formula="balance=-8.0 utilization=80%",
                proposer_balance=-8.0,
                counterparty_balance_claimed=8.0,
                utilization=0.8,
                target_utilization=0.5,
                signing_key=sk,
                storage=storage,
            )
        )
        assert isinstance(result, dict)
        assert "proof" in result
        assert result["proof"]["verificationMethod"] == f"did:knarr:{NODE_ID}#key-1"

    def test_prepare_has_document_fields(self):
        sk = _make_signing_key()
        storage = _make_storage()
        result = asyncio.get_event_loop().run_until_complete(
            prepare_settlement(
                node_id=NODE_ID,
                peer_key=PEER_KEY,
                amount=25.0,
                formula="test formula",
                proposer_balance=-5.0,
                counterparty_balance_claimed=5.0,
                utilization=0.9,
                target_utilization=0.5,
                signing_key=sk,
                storage=storage,
            )
        )
        assert result["document_type"] == "settlement_prepared"
        assert result["proposer"] == NODE_ID
        assert result["counterparty"] == PEER_KEY
        assert result["amount"] == 25.0

    def test_prepare_signature_verifiable(self):
        sk = _make_signing_key()
        storage = _make_storage()
        result = asyncio.get_event_loop().run_until_complete(
            prepare_settlement(
                node_id=NODE_ID,
                peer_key=PEER_KEY,
                amount=30.0,
                formula="test",
                proposer_balance=-6.0,
                counterparty_balance_claimed=6.0,
                utilization=0.85,
                target_utilization=0.5,
                signing_key=sk,
                storage=storage,
            )
        )
        verify_key = sk.verify_key
        assert verify_document(result, verify_key)

    def test_prepare_writes_receipt(self):
        sk = _make_signing_key()
        storage = _make_storage()
        asyncio.get_event_loop().run_until_complete(
            prepare_settlement(
                node_id=NODE_ID,
                peer_key=PEER_KEY,
                amount=40.0,
                formula="test",
                proposer_balance=-7.0,
                counterparty_balance_claimed=7.0,
                utilization=0.88,
                target_utilization=0.5,
                signing_key=sk,
                storage=storage,
            )
        )
        storage.write_receipt.assert_called_once()
        call_kwargs = storage.write_receipt.call_args
        assert call_kwargs[1]["document_type"] == "settlement_prepared"

    def test_prepare_emits_bus_event(self):
        sk = _make_signing_key()
        storage = _make_storage()
        bus = _make_bus()
        asyncio.get_event_loop().run_until_complete(
            prepare_settlement(
                node_id=NODE_ID,
                peer_key=PEER_KEY,
                amount=20.0,
                formula="test",
                proposer_balance=-4.0,
                counterparty_balance_claimed=4.0,
                utilization=0.82,
                target_utilization=0.5,
                signing_key=sk,
                storage=storage,
                bus=bus,
            )
        )
        bus.emit.assert_called_once()
        event_name = bus.emit.call_args[0][0]
        assert event_name == "settlement.prepared"


class TestValidateDualSignatures:
    def _make_signed_doc(self, sk: SigningKey, verification_method: str) -> dict:
        payload = {
            "document_type": "settlement_prepared",
            "proposer": NODE_ID,
            "counterparty": PEER_KEY,
            "amount": 50.0,
            "formula": "test",
            "proposer_balance": -8.0,
            "counterparty_balance_claimed": 8.0,
            "utilization": 0.85,
            "target_utilization": 0.5,
            "receipt_id": "sp_test",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "version": 2,
        }
        return sign_document(payload, sk, verification_method)

    def test_valid_dual_signatures(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()
        node_vm = f"did:knarr:{NODE_ID}#key-1"
        auth_vm = f"did:knarr:{NODE_ID}#cockpit-1"

        prepared = self._make_signed_doc(node_sk, node_vm)
        # Build countersigned: same payload, different proof
        payload_only = {k: v for k, v in prepared.items() if k != "proof"}
        countersigned = sign_document(payload_only, auth_sk, auth_vm)

        ok, reason = validate_dual_signatures(
            prepared, countersigned, node_sk.verify_key, auth_sk.verify_key
        )
        assert ok, f"Expected valid dual signatures: {reason}"
        assert reason == ""

    def test_node_signature_wrong_key_rejected(self):
        node_sk = _make_signing_key()
        wrong_sk = _make_signing_key()
        auth_sk = _make_signing_key()

        prepared = self._make_signed_doc(node_sk, f"did:knarr:{NODE_ID}#key-1")
        payload_only = {k: v for k, v in prepared.items() if k != "proof"}
        countersigned = sign_document(payload_only, auth_sk, f"did:knarr:{NODE_ID}#cockpit-1")

        ok, reason = validate_dual_signatures(
            prepared, countersigned, wrong_sk.verify_key, auth_sk.verify_key
        )
        assert not ok
        assert "#key-1" in reason or "Node signature" in reason

    def test_authority_signature_wrong_key_rejected(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()
        wrong_auth_sk = _make_signing_key()

        prepared = self._make_signed_doc(node_sk, f"did:knarr:{NODE_ID}#key-1")
        payload_only = {k: v for k, v in prepared.items() if k != "proof"}
        countersigned = sign_document(payload_only, auth_sk, f"did:knarr:{NODE_ID}#cockpit-1")

        ok, reason = validate_dual_signatures(
            prepared, countersigned, node_sk.verify_key, wrong_auth_sk.verify_key
        )
        assert not ok
        assert "Authority" in reason

    def test_tampered_payload_rejected(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()

        prepared = self._make_signed_doc(node_sk, f"did:knarr:{NODE_ID}#key-1")
        payload_only = {k: v for k, v in prepared.items() if k != "proof"}
        countersigned = sign_document(payload_only, auth_sk, f"did:knarr:{NODE_ID}#cockpit-1")

        # Tamper with the countersigned payload
        tampered = dict(countersigned)
        tampered["amount"] = 9999.0

        ok, reason = validate_dual_signatures(
            prepared, tampered, node_sk.verify_key, auth_sk.verify_key
        )
        assert not ok

    def test_missing_proof_rejected(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()

        prepared = self._make_signed_doc(node_sk, f"did:knarr:{NODE_ID}#key-1")
        no_proof = {k: v for k, v in prepared.items() if k != "proof"}

        ok, reason = validate_dual_signatures(
            no_proof, prepared, node_sk.verify_key, auth_sk.verify_key
        )
        assert not ok
        assert "missing proof" in reason


class TestExecuteSettlement:
    def _make_signed_pair(self, node_sk, auth_sk):
        payload = {
            "document_type": "settlement_prepared",
            "proposer": NODE_ID,
            "counterparty": PEER_KEY,
            "amount": 50.0,
            "formula": "test",
            "proposer_balance": -8.0,
            "counterparty_balance_claimed": 8.0,
            "utilization": 0.85,
            "target_utilization": 0.5,
            "receipt_id": "sp_test01",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "version": 2,
        }
        prepared = sign_document(payload, node_sk, f"did:knarr:{NODE_ID}#key-1")
        payload_only = {k: v for k, v in prepared.items() if k != "proof"}
        countersigned = sign_document(payload_only, auth_sk, f"did:knarr:{NODE_ID}#cockpit-1")
        return prepared, countersigned

    def test_execute_writes_receipt(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()
        prepared, countersigned = self._make_signed_pair(node_sk, auth_sk)
        storage = _make_storage()

        async def _send(*args, **kwargs):
            pass

        receipt_id = asyncio.get_event_loop().run_until_complete(
            execute_settlement(
                prepared_doc=prepared,
                countersigned_doc=countersigned,
                node_verify_key=node_sk.verify_key,
                authority_verify_key=auth_sk.verify_key,
                node_id=NODE_ID,
                signing_key=node_sk,
                peer_key=PEER_KEY,
                storage=storage,
                send_mail_fn=_send,
            )
        )
        assert isinstance(receipt_id, str)
        assert receipt_id.startswith("sa_")
        storage.write_receipt.assert_called()

    def test_execute_emits_bus_event(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()
        prepared, countersigned = self._make_signed_pair(node_sk, auth_sk)
        storage = _make_storage()
        bus = _make_bus()

        async def _send(*args, **kwargs):
            pass

        asyncio.get_event_loop().run_until_complete(
            execute_settlement(
                prepared_doc=prepared,
                countersigned_doc=countersigned,
                node_verify_key=node_sk.verify_key,
                authority_verify_key=auth_sk.verify_key,
                node_id=NODE_ID,
                signing_key=node_sk,
                peer_key=PEER_KEY,
                storage=storage,
                send_mail_fn=_send,
                bus=bus,
            )
        )
        bus.emit.assert_called()
        event_name = bus.emit.call_args[0][0]
        assert event_name == "settlement.accepted"

    def test_execute_bad_signatures_raises(self):
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()
        wrong_sk = _make_signing_key()
        prepared, countersigned = self._make_signed_pair(node_sk, auth_sk)
        storage = _make_storage()

        async def _send(*args, **kwargs):
            pass

        with pytest.raises(ValueError, match="Dual signature"):
            asyncio.get_event_loop().run_until_complete(
                execute_settlement(
                    prepared_doc=prepared,
                    countersigned_doc=countersigned,
                    node_verify_key=wrong_sk.verify_key,  # wrong key
                    authority_verify_key=auth_sk.verify_key,
                    node_id=NODE_ID,
                    signing_key=node_sk,
                    peer_key=PEER_KEY,
                    storage=storage,
                    send_mail_fn=_send,
                )
            )

    def test_bus_payload_contains_full_document(self):
        """WM-readiness: bus events must carry full Document + Proof in payload_json."""
        node_sk = _make_signing_key()
        auth_sk = _make_signing_key()
        prepared, countersigned = self._make_signed_pair(node_sk, auth_sk)
        storage = _make_storage()
        bus = _make_bus()

        async def _send(*args, **kwargs):
            pass

        asyncio.get_event_loop().run_until_complete(
            execute_settlement(
                prepared_doc=prepared,
                countersigned_doc=countersigned,
                node_verify_key=node_sk.verify_key,
                authority_verify_key=auth_sk.verify_key,
                node_id=NODE_ID,
                signing_key=node_sk,
                peer_key=PEER_KEY,
                storage=storage,
                send_mail_fn=_send,
                bus=bus,
            )
        )
        call_kwargs = bus.emit.call_args[1]
        payload_json_str = call_kwargs.get("payload_json", "")
        assert payload_json_str, "payload_json must be present in bus event"
        payload = json.loads(payload_json_str)
        assert "proof" in payload, "Full document with proof must be in payload_json"
