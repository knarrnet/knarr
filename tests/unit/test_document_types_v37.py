"""Tests for v0.37.0 Track C: Eight new document types."""

import pytest

from knarr.commerce.documents import (
    Document,
    payment_received,
    payment_finalized,
    payment_executed,
    wallet_transfer,
    wallet_withdrawal,
    configuration_order,
    punchhole_card,
    cache_object,
)
from knarr.commerce.schemas import (
    validate_payment_received,
    validate_payment_finalized,
    validate_payment_executed,
    validate_wallet_transfer,
    validate_wallet_withdrawal,
    validate_configuration_order,
    validate_punchhole_card,
    validate_cache_object,
)


# ── Document Construction ─────────────────────────────────────────────


class TestPaymentReceived:
    def test_construction(self):
        doc = payment_received(
            chain_id="solana-mainnet", tx_hash="abc123", tx_index=0,
            from_address="Sender1", to_address="Recvr1",
            amount=1000000, denom="KNARR", decimals=9,
            confirmation={"level": "finalized"},
        )
        assert doc.document_type == "payment_received"
        assert doc["receipt_id"].startswith("prx_")
        assert doc["amount"] == 1000000

    def test_missing_field(self):
        with pytest.raises(ValueError, match="Missing required"):
            Document("payment_received", {
                "chain_id": "solana-mainnet", "tx_hash": "abc123", "tx_index": 0,
                "from_address": "Sender1", "to_address": "Recvr1",
                "amount": 1000000, "denom": "KNARR", "decimals": 9,
                # confirmation intentionally omitted
            })

    def test_extra_fields(self):
        doc = payment_received(
            chain_id="solana-mainnet", tx_hash="abc", tx_index=0,
            from_address="S", to_address="R",
            amount=100, denom="SOL", decimals=9,
            confirmation={"level": "finalized"},
            attribution={"derived_for_node": "abc123"},
        )
        assert doc["attribution"]["derived_for_node"] == "abc123"


class TestPaymentFinalized:
    def test_construction(self):
        doc = payment_finalized(
            chain_id="solana-mainnet", tx_hash="abc123",
            amount=1000000, denom="KNARR",
            original_receipt_id="prx_12345678",
            finality={"level": "finalized", "confirmations": 32},
        )
        assert doc["receipt_id"].startswith("pfin_")
        assert doc["original_receipt_id"] == "prx_12345678"


class TestPaymentExecuted:
    def test_construction(self):
        doc = payment_executed(
            chain_id="solana-mainnet", tx_hash="abc",
            from_address="S", to_address="R",
            amount=500000, denom="KNARR", decimals=9,
            settlement_ref={"settlement_accepted_id": "sa_123"},
            finality={"level": "finalized"},
        )
        assert doc["receipt_id"].startswith("pexe_")


class TestWalletTransfer:
    def test_construction(self):
        doc = wallet_transfer(
            chain_id="solana-mainnet", tx_hash="abc",
            from_address="Master", to_address="Derived",
            amount=100000, denom="SOL", decimals=9,
            transfer_type="master_to_derived",
        )
        assert doc["receipt_id"].startswith("wtfr_")
        assert doc["transfer_type"] == "master_to_derived"


class TestWalletWithdrawal:
    def test_construction(self):
        doc = wallet_withdrawal(
            chain_id="solana-mainnet", tx_hash="abc",
            from_address="Master", to_address="External",
            amount=50000, denom="SOL", decimals=9,
        )
        assert doc["receipt_id"].startswith("wwdr_")


class TestConfigurationOrder:
    def test_construction(self):
        doc = configuration_order(
            target="exposure_schema",
            operation="upsert_object",
            changes={"object_key": "economy.summary", "access": "known_hosts"},
            reason="Add economy summary disclosure",
        )
        assert doc["receipt_id"].startswith("cord_")
        assert doc["operation"] == "upsert_object"


class TestPunchholeCard:
    def test_construction(self):
        doc = punchhole_card(
            for_node="abc123",
            for_access_level="known_hosts",
            available=[{"key": "economy.summary", "fields": ["balance"]}],
            not_available=[{"key": "economy.bilateral", "reason": "peer tier required"}],
        )
        assert doc["receipt_id"].startswith("pcard_")
        assert len(doc["available"]) == 1


class TestCacheObject:
    def test_construction(self):
        doc = cache_object(
            object_key="economy.summary",
            data={"credit_balance": 1200, "settlement_count": 5},
            granularity={"credit_balance": "range:50", "settlement_count": "exact"},
        )
        assert doc["receipt_id"].startswith("cobj_")
        assert doc["data"]["credit_balance"] == 1200


# ── Schema Validators ─────────────────────────────────────────────────


class TestPaymentReceivedValidator:
    def test_valid(self):
        ok, err = validate_payment_received({
            "chain_id": "solana-mainnet", "tx_hash": "abc", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": 1000, "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        })
        assert ok

    def test_missing_chain_id(self):
        ok, err = validate_payment_received({
            "tx_hash": "abc", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": 1000, "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        })
        assert not ok

    def test_zero_amount(self):
        ok, err = validate_payment_received({
            "chain_id": "solana-mainnet", "tx_hash": "abc", "tx_index": 0,
            "from_address": "S", "to_address": "R",
            "amount": 0, "denom": "KNARR", "decimals": 9,
            "confirmation": {"level": "finalized"},
        })
        assert not ok
        assert "positive" in err


class TestPaymentFinalizedValidator:
    def test_finality_required(self):
        ok, err = validate_payment_finalized({
            "chain_id": "solana-mainnet", "tx_hash": "abc",
            "amount": 1000, "denom": "KNARR",
            "original_receipt_id": "prx_123",
            "finality": {"level": "confirmed"},
        })
        assert not ok
        assert "finalized" in err


class TestWalletTransferValidator:
    def test_invalid_transfer_type(self):
        ok, err = validate_wallet_transfer({
            "chain_id": "solana-mainnet", "tx_hash": "abc",
            "from_address": "S", "to_address": "R",
            "amount": 100, "denom": "SOL", "decimals": 9,
            "transfer_type": "unknown_type",
        })
        assert not ok
        assert "transfer_type" in err


class TestConfigurationOrderValidator:
    def test_valid(self):
        ok, err = validate_configuration_order({
            "target": "exposure_schema",
            "operation": "upsert_object",
            "changes": {"object_key": "economy.summary"},
        })
        assert ok

    def test_invalid_operation(self):
        ok, err = validate_configuration_order({
            "target": "exposure_schema",
            "operation": "drop_table",
            "changes": {},
        })
        assert not ok
        assert "operation" in err


class TestPunchholeCardValidator:
    def test_valid(self):
        ok, err = validate_punchhole_card({
            "for_node": "abc",
            "for_access_level": "peer",
            "available": [],
            "not_available": [],
        })
        assert ok

    def test_available_must_be_list(self):
        ok, err = validate_punchhole_card({
            "for_node": "abc",
            "for_access_level": "peer",
            "available": "not a list",
            "not_available": [],
        })
        assert not ok


class TestCacheObjectValidator:
    def test_valid(self):
        ok, err = validate_cache_object({
            "object_key": "economy.summary",
            "data": {"balance": 100},
            "granularity": {"balance": "exact"},
        })
        assert ok

    def test_data_must_be_dict(self):
        ok, err = validate_cache_object({
            "object_key": "economy.summary",
            "data": "not a dict",
            "granularity": {},
        })
        assert not ok
