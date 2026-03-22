from types import SimpleNamespace
from unittest.mock import MagicMock

from knarr.commerce.handlers import _resolve_public_key
from knarr.dht.storage import Storage


def test_get_pubkey_by_node_id_returns_indexed_mapping():
    storage = Storage(":memory:")
    try:
        public_key = "ab" * 32
        node_id = storage.get_node_id_for_public_key(public_key)
        storage.get_or_create_ledger_entry(public_key, 0.0, 0.3)

        assert storage.get_pubkey_by_node_id(node_id) == public_key
    finally:
        storage.close()


def test_resolve_public_key_uses_indexed_lookup_not_full_scan():
    storage = MagicMock()
    storage.get_pubkey_by_node_id.return_value = "cd" * 32
    storage.get_all_ledger_entries.side_effect = AssertionError("full scan should not be used")
    node = SimpleNamespace(storage=storage)

    result = _resolve_public_key(node, "ef" * 32)

    assert result == "cd" * 32
    storage.get_pubkey_by_node_id.assert_called_once_with("ef" * 32)
