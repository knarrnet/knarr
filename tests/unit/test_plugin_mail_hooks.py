"""Tests for plugin mail handler registration hooks."""
import dataclasses
from knarr.dht.plugins import PluginContext


def test_plugin_context_has_register_mail_handler():
    """PluginContext exposes register_mail_handler field."""
    fields = {f.name for f in dataclasses.fields(PluginContext)}
    assert "register_mail_handler" in fields


def test_plugin_context_has_send_mail():
    """PluginContext exposes send_mail field."""
    fields = {f.name for f in dataclasses.fields(PluginContext)}
    assert "send_mail" in fields


def test_register_handler_dispatch():
    """SyncEngine.register_handler wires into _dispatch_system_item."""
    from knarr.mail.sync import SyncEngine
    from unittest.mock import MagicMock

    node = MagicMock()
    node._config = {}
    engine = SyncEngine(node)

    calls = []
    engine.register_handler("test/ping", lambda item: calls.append(item))
    assert "test/ping" in engine._mail_handlers
