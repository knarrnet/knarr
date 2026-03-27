"""E-03: IdentityRegistry class.

Tests:
1. Identity can be constructed with name, node_id.
2. register_skill adds skill to identity.skills.
3. deregister_skill removes skill and returns True.
4. deregister_skill returns False when skill not present.
5. IdentityRegistry.register stores identity by node_id.
6. IdentityRegistry.resolve returns correct identity.
7. IdentityRegistry.resolve returns None for unknown node_id.
8. IdentityRegistry.default returns the first registered identity.
9. IdentityRegistry.resolve_by_name finds identity by name.
10. IdentityRegistry.all returns all identities.
11. len(registry) returns count.
12. node_id 'in' registry works.
"""
import pytest


class TestIdentity:
    def test_identity_construction(self):
        """Identity can be constructed with name and node_id."""
        from knarr.dht.identities import Identity
        ident = Identity(name="alice", node_id="aa" * 32)
        assert ident.name == "alice"
        assert ident.node_id == "aa" * 32
        assert ident.skills == {}
        assert ident.plugins == set()

    def test_register_skill(self):
        """register_skill adds handler to identity.skills."""
        from knarr.dht.identities import Identity
        ident = Identity(name="alice", node_id="aa" * 32)
        handler = object()
        ident.register_skill("my_skill", handler)
        assert ident.skills["my_skill"] is handler

    def test_deregister_skill_returns_true(self):
        """deregister_skill returns True when skill existed."""
        from knarr.dht.identities import Identity
        ident = Identity(name="alice", node_id="aa" * 32)
        ident.register_skill("my_skill", object())
        assert ident.deregister_skill("my_skill") is True
        assert "my_skill" not in ident.skills

    def test_deregister_skill_returns_false_when_absent(self):
        """deregister_skill returns False when skill not present."""
        from knarr.dht.identities import Identity
        ident = Identity(name="alice", node_id="aa" * 32)
        assert ident.deregister_skill("nonexistent") is False

    def test_identity_optional_fields(self):
        """Optional fields (bus, storage, vault) default to None."""
        from knarr.dht.identities import Identity
        ident = Identity(name="bob", node_id="bb" * 32)
        assert ident.bus is None
        assert ident.storage is None
        assert ident.vault is None
        assert ident.signing_key is None

    def test_identity_data_dir_stored(self):
        """data_dir is stored on Identity."""
        from knarr.dht.identities import Identity
        ident = Identity(name="alice", node_id="aa" * 32, data_dir="/data/identity-alice")
        assert ident.data_dir == "/data/identity-alice"


class TestIdentityRegistry:
    def test_register_and_resolve(self):
        """register stores identity, resolve returns it by node_id."""
        from knarr.dht.identities import Identity, IdentityRegistry
        registry = IdentityRegistry()
        ident = Identity(name="alice", node_id="aa" * 32)
        registry.register(ident)
        assert registry.resolve("aa" * 32) is ident

    def test_resolve_unknown_returns_none(self):
        """resolve returns None for an unknown node_id."""
        from knarr.dht.identities import IdentityRegistry
        registry = IdentityRegistry()
        assert registry.resolve("bb" * 32) is None

    def test_default_is_first_registered(self):
        """The first registered identity becomes the default."""
        from knarr.dht.identities import Identity, IdentityRegistry
        registry = IdentityRegistry()
        alice = Identity(name="alice", node_id="aa" * 32)
        bob = Identity(name="bob", node_id="bb" * 32)
        registry.register(alice)
        registry.register(bob)
        assert registry.default is alice

    def test_default_node_id_at_construction(self):
        """default_node_id at construction sets the default."""
        from knarr.dht.identities import Identity, IdentityRegistry
        alice = Identity(name="alice", node_id="aa" * 32)
        bob = Identity(name="bob", node_id="bb" * 32)
        registry = IdentityRegistry(default_node_id="bb" * 32)
        registry.register(alice)
        registry.register(bob)
        assert registry.default is bob

    def test_resolve_by_name(self):
        """resolve_by_name finds identity by human-readable name."""
        from knarr.dht.identities import Identity, IdentityRegistry
        registry = IdentityRegistry()
        alice = Identity(name="alice", node_id="aa" * 32)
        registry.register(alice)
        assert registry.resolve_by_name("alice") is alice
        assert registry.resolve_by_name("nobody") is None

    def test_all_returns_all_identities(self):
        """all property returns list of all registered identities."""
        from knarr.dht.identities import Identity, IdentityRegistry
        registry = IdentityRegistry()
        alice = Identity(name="alice", node_id="aa" * 32)
        bob = Identity(name="bob", node_id="bb" * 32)
        registry.register(alice)
        registry.register(bob)
        all_ids = registry.all
        assert len(all_ids) == 2
        assert alice in all_ids
        assert bob in all_ids

    def test_len_returns_count(self):
        """len(registry) returns number of registered identities."""
        from knarr.dht.identities import Identity, IdentityRegistry
        registry = IdentityRegistry()
        assert len(registry) == 0
        registry.register(Identity(name="alice", node_id="aa" * 32))
        assert len(registry) == 1

    def test_contains_operator(self):
        """'node_id in registry' checks if identity is registered."""
        from knarr.dht.identities import Identity, IdentityRegistry
        registry = IdentityRegistry()
        registry.register(Identity(name="alice", node_id="aa" * 32))
        assert "aa" * 32 in registry
        assert "bb" * 32 not in registry

    def test_empty_registry_default_is_none(self):
        """default returns None on empty registry."""
        from knarr.dht.identities import IdentityRegistry
        registry = IdentityRegistry()
        assert registry.default is None
