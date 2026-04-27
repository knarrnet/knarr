import asyncio
import hashlib

import pytest

from knarr.commerce.settlement_execution import execute_settlement
from knarr.core.crypto import SigningKey
from knarr.core.proof import sign_document
from knarr.dht.storage import Storage


NODE_ID = "a" * 64
PEER_A = "b" * 64
PEER_B = "c" * 64


def _signed_pair(node_sk, auth_sk, *, peer_key=PEER_A, receipt_id="sp-a06", amount=10.0):
    payload = {
        "document_type": "settlement_prepared",
        "proposer": NODE_ID,
        "counterparty": peer_key,
        "amount": amount,
        "formula": "test",
        "proposer_balance": -amount,
        "counterparty_balance_claimed": amount,
        "utilization": 0.8,
        "target_utilization": 0.5,
        "receipt_id": receipt_id,
        "timestamp": "2026-04-22T00:00:00.000Z",
        "version": 2,
    }
    prepared = sign_document(payload, node_sk, f"did:knarr:{NODE_ID}#key-1")
    countersigned = sign_document(
        {k: v for k, v in prepared.items() if k != "proof"},
        auth_sk,
        f"did:knarr:{NODE_ID}#cockpit-1",
    )
    return prepared, countersigned


def _register_peer_key(storage, peer_key):
    node_id = hashlib.sha256(bytes.fromhex(peer_key)).hexdigest()
    conn = storage._get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO peer_keys (node_id, public_key) VALUES (?, ?)",
        (node_id, peer_key),
    )
    conn.commit()
    return node_id


def _ledger_count(storage, peer_key=None):
    conn = storage._get_conn()
    if peer_key is None:
        return conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE peer_public_key = ?",
        (peer_key,),
    ).fetchone()[0]


def _accepted_receipt_count(storage):
    return storage._get_conn().execute(
        "SELECT COUNT(*) FROM receipt_log WHERE document_type = 'settlement_accepted'"
    ).fetchone()[0]


def _serialized_enqueue_recorder(calls):
    lock = asyncio.Lock()

    async def enqueue_write(op, *args):
        async with lock:
            calls.append(getattr(op, "__name__", repr(op)))
            return op(*args)

    return enqueue_write


async def _run_execute(storage, node_sk, auth_sk, peer_key, enqueue_write, sent, receipt_suffix):
    prepared, countersigned = _signed_pair(
        node_sk,
        auth_sk,
        peer_key=peer_key,
        receipt_id=f"sp-a06-{receipt_suffix}",
    )

    async def send_mail_fn(**kwargs):
        sent.append(kwargs)

    return await execute_settlement(
        prepared_doc=prepared,
        countersigned_doc=countersigned,
        node_verify_key=node_sk.verify_key,
        authority_verify_key=auth_sk.verify_key,
        node_id=NODE_ID,
        signing_key=node_sk,
        peer_key=peer_key,
        storage=storage,
        send_mail_fn=send_mail_fn,
        enqueue_write=enqueue_write,
    )


@pytest.mark.asyncio
async def test_two_concurrent_settlements_same_pair_create_one_ledger_entry(tmp_path):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        _register_peer_key(storage, PEER_A)
        node_sk = SigningKey.generate()
        auth_sk = SigningKey.generate()
        calls = []
        sent = []
        enqueue_write = _serialized_enqueue_recorder(calls)

        await asyncio.gather(
            _run_execute(storage, node_sk, auth_sk, PEER_A, enqueue_write, sent, "one"),
            _run_execute(storage, node_sk, auth_sk, PEER_A, enqueue_write, sent, "two"),
        )

        assert _ledger_count(storage, PEER_A) == 1
        assert _accepted_receipt_count(storage) == 2
        assert len(sent) == 2
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_concurrent_settlements_different_pairs_create_separate_entries(tmp_path):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        _register_peer_key(storage, PEER_A)
        _register_peer_key(storage, PEER_B)
        node_sk = SigningKey.generate()
        auth_sk = SigningKey.generate()
        calls = []
        sent = []
        enqueue_write = _serialized_enqueue_recorder(calls)

        await asyncio.gather(
            _run_execute(storage, node_sk, auth_sk, PEER_A, enqueue_write, sent, "a"),
            _run_execute(storage, node_sk, auth_sk, PEER_B, enqueue_write, sent, "b"),
        )

        assert _ledger_count(storage) == 2
        assert _ledger_count(storage, PEER_A) == 1
        assert _ledger_count(storage, PEER_B) == 1
        assert _accepted_receipt_count(storage) == 2
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_settlement_writes_flow_through_enqueue_write_path(tmp_path):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        _register_peer_key(storage, PEER_A)
        node_sk = SigningKey.generate()
        auth_sk = SigningKey.generate()
        calls = []
        sent = []

        await _run_execute(
            storage,
            node_sk,
            auth_sk,
            PEER_A,
            _serialized_enqueue_recorder(calls),
            sent,
            "observed",
        )

        assert "_write_accepted_receipt" in calls
        assert "get_or_create_ledger_entry" in calls
        assert calls.index("_write_accepted_receipt") < calls.index("get_or_create_ledger_entry")
    finally:
        storage.close()
