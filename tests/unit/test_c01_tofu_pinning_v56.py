import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knarr.cli.main import cmd_peer
from knarr.core.crypto import SigningKey
from knarr.core.messages import Heartbeat
from knarr.dht.node import CertPinMismatchError, DHTNode
from knarr.dht.pool import ConnectionPool
from knarr.dht.protocol import request_response
from knarr.dht.storage import Storage


class _FakeSSLObject:
    def __init__(self, cert_bytes: bytes):
        self._cert_bytes = cert_bytes

    def getpeercert(self, binary_form=False):
        assert binary_form is True
        return self._cert_bytes


class _FakeWriter:
    def __init__(self, cert_bytes: bytes):
        self._ssl_object = _FakeSSLObject(cert_bytes)

    def get_extra_info(self, name):
        if name == "ssl_object":
            return self._ssl_object
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None

    def is_closing(self):
        return False


def _signed_peer_message():
    signing_key = SigningKey.generate()
    public_key = signing_key.verify_key.encode().hex()
    node_id = hashlib.sha256(signing_key.verify_key.encode()).hexdigest()
    return Heartbeat(node_id=node_id, public_key=public_key), node_id


def _make_node(storage, bus=None):
    node = DHTNode.__new__(DHTNode)
    node._config = {"node": {"tls_pin_certs": True}}
    node._base_storage = storage
    node._base_bus = bus if bus is not None else MagicMock()
    return node


def test_storage_helpers_round_trip_tls_fingerprint(tmp_path):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        node_id = "a" * 64
        storage.set_peer_cert_fingerprint(node_id, "fp-1", host="127.0.0.1", port=9010)
        assert storage.get_peer_cert_fingerprint(node_id) == "fp-1"
        assert storage.clear_peer_cert_fingerprint(node_id) is True
        assert storage.get_peer_cert_fingerprint(node_id) == ""
    finally:
        storage.close()


def test_check_tls_peer_fingerprint_logs_cert_pin_stored_on_first_contact(tmp_path, caplog):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        msg, node_id = _signed_peer_message()
        object.__setattr__(msg, "_tls_peer_cert_fingerprint", "fp-1")
        object.__setattr__(msg, "_tls_peer_host", "127.0.0.1")
        object.__setattr__(msg, "_tls_peer_port", 9010)

        with caplog.at_level("INFO", logger="knarr.dht.node"):
            _make_node(storage)._check_tls_peer_fingerprint(msg)

        assert storage.get_peer_cert_fingerprint(node_id) == "fp-1"
        assert any(
            record.levelname == "INFO"
            and record.getMessage() == f"CERT_PIN_STORED node_id={node_id} fingerprint=fp-1"
            for record in caplog.records
        )
    finally:
        storage.close()


def test_check_tls_peer_fingerprint_pins_first_contact_and_accepts_match(tmp_path):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        msg, node_id = _signed_peer_message()
        object.__setattr__(msg, "_tls_peer_cert_fingerprint", "fp-1")
        object.__setattr__(msg, "_tls_peer_host", "127.0.0.1")
        object.__setattr__(msg, "_tls_peer_port", 9010)

        node = DHTNode.__new__(DHTNode)
        node._config = {"node": {"tls_pin_certs": True}}
        node._base_storage = storage
        node._base_bus = MagicMock()

        node._check_tls_peer_fingerprint(msg)
        assert storage.get_peer_cert_fingerprint(node_id) == "fp-1"

        node._check_tls_peer_fingerprint(msg)
        assert storage.get_peer_cert_fingerprint(node_id) == "fp-1"
    finally:
        storage.close()


def test_check_tls_peer_fingerprint_logs_cert_pin_match_on_subsequent_contact(tmp_path, caplog):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        msg, node_id = _signed_peer_message()
        storage.set_peer_cert_fingerprint(node_id, "fp-1", host="127.0.0.1", port=9010)
        object.__setattr__(msg, "_tls_peer_cert_fingerprint", "fp-1")
        object.__setattr__(msg, "_tls_peer_host", "127.0.0.1")
        object.__setattr__(msg, "_tls_peer_port", 9010)

        with caplog.at_level("DEBUG", logger="knarr.dht.node"):
            _make_node(storage)._check_tls_peer_fingerprint(msg)

        assert any(
            record.levelname == "DEBUG" and record.getMessage() == f"CERT_PIN_MATCH node_id={node_id}"
            for record in caplog.records
        )
    finally:
        storage.close()


def test_check_tls_peer_fingerprint_rejects_mismatch_and_emits_event(tmp_path, caplog):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        msg, node_id = _signed_peer_message()
        storage.set_peer_cert_fingerprint(node_id, "fp-old", host="127.0.0.1", port=9010)
        object.__setattr__(msg, "_tls_peer_cert_fingerprint", "fp-new")
        object.__setattr__(msg, "_tls_peer_host", "127.0.0.1")
        object.__setattr__(msg, "_tls_peer_port", 9010)

        bus = MagicMock()
        node = _make_node(storage, bus=bus)

        with caplog.at_level("WARNING", logger="knarr.dht.node"):
            with pytest.raises(CertPinMismatchError):
                node._check_tls_peer_fingerprint(msg)

        assert any(
            record.levelname == "WARNING"
            and record.getMessage()
            == f"CERT_PIN_MISMATCH node_id={node_id} stored=fp-old got=fp-new reject=true"
            for record in caplog.records
        )
        assert bus.emit.call_args.args == ("security.cert_pinning_mismatch",)
        assert bus.emit.call_args.kwargs == {
            "node_id": node_id,
            "stored_fingerprint": "fp-old",
            "presented_fingerprint": "fp-new",
            "identity": node_id,
        }
    finally:
        storage.close()


def test_check_tls_peer_fingerprint_treats_empty_legacy_pin_as_first_contact(tmp_path, caplog):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        msg, node_id = _signed_peer_message()
        storage.set_peer_cert_fingerprint(node_id, "old-fp", host="127.0.0.1", port=9010)
        assert storage.clear_peer_cert_fingerprint(node_id) is True
        assert storage.get_peer_cert_fingerprint(node_id) == ""
        object.__setattr__(msg, "_tls_peer_cert_fingerprint", "fp-new")
        object.__setattr__(msg, "_tls_peer_host", "127.0.0.1")
        object.__setattr__(msg, "_tls_peer_port", 9010)

        with caplog.at_level("INFO", logger="knarr.dht.node"):
            _make_node(storage)._check_tls_peer_fingerprint(msg)

        assert storage.get_peer_cert_fingerprint(node_id) == "fp-new"
        assert any(
            record.levelname == "INFO"
            and record.getMessage() == f"CERT_PIN_STORED node_id={node_id} fingerprint=fp-new"
            for record in caplog.records
        )
    finally:
        storage.close()


def test_peer_forget_fingerprint_cli_clears_pin(tmp_path):
    db_path = str(tmp_path / "node.db")
    storage = Storage(db_path)
    try:
        node_id = "a" * 64
        storage.set_peer_cert_fingerprint(node_id, "fp-1", host="127.0.0.1", port=9010)
    finally:
        storage.close()

    cmd_peer(SimpleNamespace(peer_command="forget-fingerprint", node_id=node_id, storage=db_path))

    storage = Storage(db_path)
    try:
        assert storage.get_peer_cert_fingerprint(node_id) == ""
    finally:
        storage.close()


def test_peer_forget_fingerprint_cli_errors_for_unknown_peer(tmp_path, capsys):
    db_path = str(tmp_path / "node.db")
    storage = Storage(db_path)
    storage.close()

    with pytest.raises(SystemExit) as exc:
        cmd_peer(SimpleNamespace(peer_command="forget-fingerprint", node_id="f" * 64, storage=db_path))

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert "not found in the peer table" in captured.err


@pytest.mark.asyncio
async def test_request_response_attaches_peer_cert_metadata():
    reader = AsyncMock()
    writer = _FakeWriter(b"request-response-cert")
    expected_fp = hashlib.sha256(b"request-response-cert").hexdigest()

    with patch("asyncio.open_connection", return_value=(reader, writer)), patch(
        "knarr.dht.protocol.send_message", new=AsyncMock()
    ), patch(
        "knarr.dht.protocol.receive_message",
        return_value=Heartbeat(node_id="peer"),
    ):
        response = await request_response("127.0.0.1", 9010, Heartbeat(node_id="self"), ssl_context=object())

    assert response._tls_peer_cert_fingerprint == expected_fp
    assert response._tls_peer_host == "127.0.0.1"
    assert response._tls_peer_port == 9010


@pytest.mark.asyncio
async def test_connection_pool_attaches_peer_cert_metadata():
    reader = AsyncMock()
    reader.at_eof = MagicMock(return_value=False)
    writer = _FakeWriter(b"pool-cert")
    expected_fp = hashlib.sha256(b"pool-cert").hexdigest()
    pool = ConnectionPool()

    with patch("asyncio.open_connection", return_value=(reader, writer)), patch(
        "knarr.dht.pool.send_message", new=AsyncMock()
    ), patch(
        "knarr.dht.pool.receive_message",
        return_value=Heartbeat(node_id="peer"),
    ):
        response = await pool.send("peer", "127.0.0.1", 9010, Heartbeat(node_id="self"))

    assert response._tls_peer_cert_fingerprint == expected_fp
    assert response._tls_peer_host == "127.0.0.1"
    assert response._tls_peer_port == 9010
