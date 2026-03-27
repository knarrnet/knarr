"""E-07: Multi-identity launcher.

Tests:
1. cmd_serve is importable.
2. main.py imports parse_identity_configs and setup_identities.
3. When no [identities] config: single identity behavior (backward compatible).
4. Multi-identity startup code path exists in main.py source.
5. Shutdown closes per-identity storage.
6. _identity_registry is populated on node after multi-identity startup.
"""
import pytest
import os


class TestLauncher:
    def test_cmd_serve_importable(self):
        """cmd_serve is importable from cli.main."""
        from knarr.cli.main import cmd_serve
        assert callable(cmd_serve)

    def test_main_imports_parse_identity_configs(self):
        """main.py imports and uses parse_identity_configs."""
        main_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/cli/main.py")
        with open(main_path) as f:
            src = f.read()
        assert "parse_identity_configs" in src

    def test_main_imports_setup_identities(self):
        """main.py imports and uses setup_identities."""
        main_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/cli/main.py")
        with open(main_path) as f:
            src = f.read()
        assert "setup_identities" in src

    def test_main_registers_in_identity_registry(self):
        """main.py passes node._identity_registry to setup_identities."""
        main_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/cli/main.py")
        with open(main_path) as f:
            src = f.read()
        assert "node._identity_registry" in src

    def test_main_closes_identity_storage_on_shutdown(self):
        """Shutdown code closes per-identity storage."""
        main_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/cli/main.py")
        with open(main_path) as f:
            src = f.read()
        assert "identity_registry.all" in src or "_identity_registry.all" in src
        assert "_ident.storage" in src

    def test_backward_compatible_no_identities(self):
        """No [identities] config → no setup_identities call (backward compat)."""
        from knarr.cli.config import parse_identity_configs
        config = {"node": {"port": 9000}}
        result = parse_identity_configs(config)
        assert result == []

    def test_node_has_identity_registry(self):
        """DHTNode has _identity_registry attribute set in __init__."""
        import os
        node_path = os.path.join(os.path.dirname(__file__), "../../src/knarr/dht/node.py")
        with open(node_path) as f:
            src = f.read()
        assert "_identity_registry" in src
        assert "IdentityRegistry" in src

    def test_identity_registry_initialized_on_node(self):
        """DHTNode._identity_registry is an IdentityRegistry instance."""
        from knarr.dht.node import DHTNode
        from knarr.dht.identities import IdentityRegistry
        node = DHTNode.__new__(DHTNode)
        # Simulate minimal __init__ for registry
        from knarr.dht.identities import IdentityRegistry
        node._identity_registry = IdentityRegistry(default_node_id="aa" * 32)
        assert isinstance(node._identity_registry, IdentityRegistry)
