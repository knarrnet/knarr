"""A1 contract test: WM Gate 1 — node's own key-1 must be in internal_signer_keys.

E-031: internal_signer_keys passed to WarehouseManager on init must include "key-1"
mapped to the node's own Ed25519 verify key. Without this, settlement_prepared
documents signed by the node itself fail Gate 1 and are quarantined.

FIX LOCATION: node.py — add before WarehouseManager.__init__:
    if self._signing_key is not None:
        internal_signer_keys["key-1"] = self._signing_key.verify_key.encode()

CONTRACT:
- When DHTNode initialises with warehouse_manager.enabled=true and a signing key,
  WarehouseManager receives internal_signer_keys containing "key-1".
- The "key-1" value must equal the node's signing_key.verify_key.encode().
"""
from unittest.mock import patch, MagicMock
import pytest
from nacl.signing import SigningKey
from knarr.dht.node import DHTNode


def _make_node_config():
    return {
        "node": {"host": "127.0.0.1", "port": 19000},
        "warehouse_manager": {"enabled": True},
        "_config_dir": ".",
    }


def test_wm_receives_key1_in_internal_signer_keys():
    """WarehouseManager must be initialised with key-1 = node verify key."""
    captured = {}

    original_wm_init = None

    def fake_wm_init(self, node_id, identity_fragments, internal_signer_keys,
                     bus, storage, config, write_receipt_cb=None):
        captured["internal_signer_keys"] = dict(internal_signer_keys)
        # Minimal stub — don't actually build the WM
        self.node_id = node_id
        self._bus = bus
        self._inbox = {}
        self._quarantine = {}
        self._config = config or {}
        self._rules = {}
        self._storage = storage
        self._write_receipt_cb = write_receipt_cb
        self._debug = False

    with patch("knarr.core.warehouse_manager.WarehouseManager.__init__", fake_wm_init), \
         patch("knarr.dht.node.DHTNode._load_config", return_value=_make_node_config()), \
         patch("knarr.dht.storage.Storage.__init__", lambda self, *a, **kw: None), \
         patch("knarr.dht.storage.Storage.get_peers", return_value=[]), \
         patch("knarr.dht.storage.Storage.run_migrations", return_value=None):

        signing_key = SigningKey.generate()
        node = DHTNode.__new__(DHTNode)
        # Inject signing key before __init__ so lean wrapper sees it
        node._signing_key = signing_key

        # Minimal init — just enough to reach the WM init block
        try:
            DHTNode.__init__(node, config=_make_node_config(), signing_key=signing_key)
        except Exception:
            pass  # May fail downstream — we only need captured to be set

    assert "key-1" in captured.get("internal_signer_keys", {}), (
        "key-1 missing from internal_signer_keys passed to WarehouseManager. "
        "Fix: add internal_signer_keys['key-1'] = self._signing_key.verify_key.encode() "
        "in node.py before WarehouseManager init."
    )
    assert captured["internal_signer_keys"]["key-1"] == signing_key.verify_key.encode(), (
        "key-1 value does not match the node signing key verify key."
    )


def test_wm_key1_value_is_verify_key_bytes():
    """The key-1 value must be raw bytes (verify_key.encode()), not hex or string."""
    captured = {}

    def fake_wm_init(self, node_id, identity_fragments, internal_signer_keys, **kwargs):
        captured["internal_signer_keys"] = dict(internal_signer_keys)
        self.node_id = node_id
        self._inbox = {}
        self._quarantine = {}
        self._config = {}
        self._rules = {}
        self._debug = False

    signing_key = SigningKey.generate()
    with patch("knarr.core.warehouse_manager.WarehouseManager.__init__", fake_wm_init), \
         patch("knarr.dht.storage.Storage.__init__", lambda self, *a, **kw: None), \
         patch("knarr.dht.storage.Storage.get_peers", return_value=[]), \
         patch("knarr.dht.storage.Storage.run_migrations", return_value=None):
        try:
            DHTNode.__init__(
                DHTNode.__new__(DHTNode),
                config=_make_node_config(),
                signing_key=signing_key,
            )
        except Exception:
            pass

    key1 = captured.get("internal_signer_keys", {}).get("key-1")
    assert isinstance(key1, bytes), (
        f"key-1 must be bytes, got {type(key1)}. Use signing_key.verify_key.encode()."
    )
    assert len(key1) == 32, f"Ed25519 verify key must be 32 bytes, got {len(key1)}."
