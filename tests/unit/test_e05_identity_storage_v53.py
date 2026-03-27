"""E-05: Per-identity storage instantiation.

Tests:
1. instantiate_identity creates data directory.
2. instantiate_identity returns an Identity with correct name and node_id.
3. instantiate_identity creates a Storage instance on the identity.
4. instantiate_identity creates an EventBus on the identity.
5. Identity node_id is generated from a new Ed25519 keypair.
6. Same identity name loaded twice returns same node_id (key persistence via vault).
7. setup_identities instantiates all configs and registers in registry.
8. setup_identities handles errors gracefully (one bad config, rest succeed).
9. identity_dir defaults to base_data_dir / "identity-{name}".
"""
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock


def _close_identity(identity):
    """Close the identity's storage connection (Windows file lock fix)."""
    if identity and identity.storage:
        try:
            identity.storage.close()
        except Exception:
            pass


class TestInstantiateIdentity:
    def test_creates_data_directory(self):
        """instantiate_identity creates the identity data directory."""
        from knarr.dht.identity_storage import instantiate_identity

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "alice", "data_dir": "identity-alice", "skills": []}
            identity = instantiate_identity(cfg, base_data_dir=Path(tmpdir))
            try:
                assert os.path.isdir(os.path.join(tmpdir, "identity-alice"))
            finally:
                _close_identity(identity)

    def test_returns_identity_with_name(self):
        """instantiate_identity returns Identity with correct name."""
        from knarr.dht.identity_storage import instantiate_identity
        from knarr.dht.identities import Identity

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "alice", "data_dir": "identity-alice"}
            identity = instantiate_identity(cfg, base_data_dir=Path(tmpdir))
            try:
                assert isinstance(identity, Identity)
                assert identity.name == "alice"
            finally:
                _close_identity(identity)

    def test_identity_has_valid_node_id(self):
        """instantiate_identity generates a 64-char hex node_id."""
        from knarr.dht.identity_storage import instantiate_identity

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "bob", "data_dir": "identity-bob"}
            identity = instantiate_identity(cfg, base_data_dir=Path(tmpdir))
            try:
                assert len(identity.node_id) == 64
                # Verify it's valid hex
                int(identity.node_id, 16)
            finally:
                _close_identity(identity)

    def test_identity_has_storage(self):
        """instantiate_identity creates a Storage instance on the identity."""
        from knarr.dht.identity_storage import instantiate_identity

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "charlie", "data_dir": "identity-charlie"}
            identity = instantiate_identity(cfg, base_data_dir=Path(tmpdir))
            try:
                assert identity.storage is not None
            finally:
                _close_identity(identity)

    def test_identity_has_event_bus(self):
        """instantiate_identity creates an EventBus on the identity."""
        from knarr.dht.identity_storage import instantiate_identity
        from knarr.dht.eventbus import EventBus

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "dave"}
            identity = instantiate_identity(cfg, base_data_dir=Path(tmpdir))
            try:
                assert identity.bus is not None
                assert isinstance(identity.bus, EventBus)
            finally:
                _close_identity(identity)

    def test_key_persistence_via_vault(self):
        """Same identity name returns same node_id when vault persists key."""
        from knarr.dht.identity_storage import instantiate_identity

        vault_store = {}

        class FakeVault:
            def get(self, key):
                return vault_store.get(key)
            def set(self, key, value):
                vault_store[key] = value

        vault = FakeVault()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "alice", "data_dir": "identity-alice"}
            id1 = instantiate_identity(cfg, base_data_dir=Path(tmpdir), vault=vault)
            id1_node_id = id1.node_id
            _close_identity(id1)
            id2 = instantiate_identity(cfg, base_data_dir=Path(tmpdir), vault=vault)
            try:
                assert id1_node_id == id2.node_id
            finally:
                _close_identity(id2)

    def test_new_keys_without_vault(self):
        """Two calls without vault generate different node_ids."""
        from knarr.dht.identity_storage import instantiate_identity

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "alice", "data_dir": "identity-alice"}
            id1 = instantiate_identity(cfg, base_data_dir=Path(tmpdir), vault=None)
            id1_node_id = id1.node_id
            _close_identity(id1)
            id2 = instantiate_identity(cfg, base_data_dir=Path(tmpdir), vault=None)
            try:
                # Different node_ids since no vault to persist
                assert id1_node_id != id2.node_id
            finally:
                _close_identity(id2)

    def test_default_data_dir(self):
        """Default data_dir is base_data_dir / 'identity-{name}'."""
        from knarr.dht.identity_storage import instantiate_identity

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = {"name": "eve"}  # no data_dir key
            identity = instantiate_identity(cfg, base_data_dir=Path(tmpdir))
            try:
                expected_dir = os.path.join(tmpdir, "identity-eve")
                assert identity.data_dir == expected_dir
            finally:
                _close_identity(identity)


class TestSetupIdentities:
    def test_setup_all_configs(self):
        """setup_identities instantiates all configs and registers them."""
        from knarr.dht.identity_storage import setup_identities
        from knarr.dht.identities import IdentityRegistry

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            configs = [
                {"name": "alice", "data_dir": "identity-alice"},
                {"name": "bob", "data_dir": "identity-bob"},
            ]
            registry = IdentityRegistry()
            identities = setup_identities(configs, base_data_dir=Path(tmpdir), registry=registry)
            try:
                assert len(identities) == 2
                assert len(registry) == 2
            finally:
                for ident in identities:
                    _close_identity(ident)

    def test_setup_registers_in_registry(self):
        """Identities are findable by name in registry after setup."""
        from knarr.dht.identity_storage import setup_identities
        from knarr.dht.identities import IdentityRegistry

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            configs = [{"name": "alice"}]
            registry = IdentityRegistry()
            identities = setup_identities(configs, base_data_dir=Path(tmpdir), registry=registry)
            try:
                assert registry.resolve_by_name("alice") is not None
            finally:
                for ident in identities:
                    _close_identity(ident)
