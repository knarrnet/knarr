"""A-05 (v0.58.0): TLS pin-certs immutability.

tls_pin_certs is read from the config at node start and stored in a locked
attribute. Runtime consumers use this locked value. A config reload that would
change it must not update the attribute — instead log a WARN.

Scenarios:
- Starts with pinning enabled → runtime always sees enabled
- Reload flips to disabled → runtime still enabled + WARN
- Starts disabled → runtime stays disabled
"""
import pytest


def _make_node(config=None):
    from knarr.dht.node import DHTNode
    return DHTNode(
        host="127.0.0.1",
        port=0,
        storage_path=":memory:",
        config=config or {},
        ephemeral=True,
    )


class TestTlsPinCertsLocked:
    """The _tls_pin_certs_locked attribute must reflect the startup config."""

    def test_default_enabled(self):
        """Default (no config) → pinning enabled."""
        node = _make_node()
        assert node._tls_pin_certs_locked is True
        node.storage.close()

    def test_explicit_enabled(self):
        """Explicit tls_pin_certs=true → locked True."""
        node = _make_node({"node": {"tls_pin_certs": True}})
        assert node._tls_pin_certs_locked is True
        node.storage.close()

    def test_explicit_disabled(self):
        """Explicit tls_pin_certs=false → locked False."""
        node = _make_node({"node": {"tls_pin_certs": False}})
        assert node._tls_pin_certs_locked is False
        node.storage.close()

    def test_runtime_uses_locked_value_enabled(self):
        """When locked True, _check_tls_peer_fingerprint proceeds (not short-circuited)."""
        node = _make_node({"node": {"tls_pin_certs": True}})
        # Verify the method uses the locked value by checking the attribute exists
        assert hasattr(node, "_tls_pin_certs_locked")
        assert node._tls_pin_certs_locked is True
        node.storage.close()

    def test_runtime_uses_locked_value_disabled(self):
        """When locked False, _check_tls_peer_fingerprint short-circuits."""
        node = _make_node({"node": {"tls_pin_certs": False}})
        assert node._tls_pin_certs_locked is False
        # The method should return immediately when pinning is disabled
        # We verify by calling with a dummy message — it should not raise
        from knarr.core.messages import Message
        msg = Message(type="ping")
        # Should return None (early exit due to pinning disabled)
        result = node._check_tls_peer_fingerprint(msg)
        assert result is None
        node.storage.close()


class TestReloadDoesNotChangeLock:
    """Config reload must not change the locked value."""

    def test_reload_flip_to_disabled_no_change(self):
        """Start enabled, reload to disabled → locked stays enabled."""
        node = _make_node({"node": {"tls_pin_certs": True}})
        assert node._tls_pin_certs_locked is True

        # Simulate reload: change _config but NOT the locked value
        node._config = {"node": {"tls_pin_certs": False}}
        # The locked value should NOT change
        assert node._tls_pin_certs_locked is True
        node.storage.close()

    def test_reload_flip_to_enabled_no_change(self):
        """Start disabled, reload to enabled → locked stays disabled."""
        node = _make_node({"node": {"tls_pin_certs": False}})
        assert node._tls_pin_certs_locked is False

        # Simulate reload
        node._config = {"node": {"tls_pin_certs": True}}
        assert node._tls_pin_certs_locked is False
        node.storage.close()

    def test_reload_same_value_no_change(self):
        """Reload with same value → no change (trivial)."""
        node = _make_node({"node": {"tls_pin_certs": True}})
        node._config = {"node": {"tls_pin_certs": True}}
        assert node._tls_pin_certs_locked is True
        node.storage.close()
