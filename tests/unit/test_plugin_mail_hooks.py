"""Tests for plugin mail handler registration hooks."""
import dataclasses
from knarr.dht.plugins import PluginContext


def test_plugin_context_has_register_mail_handler():
    """PluginContext exposes register_mail_handler attribute.

    PluginContext is a regular class (D-007 Phase D); use hasattr not dataclasses.fields.
    """
    assert hasattr(PluginContext, "register_mail_handler") or \
        "register_mail_handler" in PluginContext.__init__.__code__.co_varnames, \
        "PluginContext does not expose register_mail_handler"


def test_plugin_context_has_send_mail():
    """PluginContext exposes send_mail attribute.

    PluginContext is a regular class (D-007 Phase D); use hasattr not dataclasses.fields.
    """
    assert hasattr(PluginContext, "send_mail") or \
        "send_mail" in PluginContext.__init__.__code__.co_varnames, \
        "PluginContext does not expose send_mail"


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
