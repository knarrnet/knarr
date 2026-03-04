"""Tests for PluginHooks additions (on_settlement_review, on_inbound_settlement)."""

import asyncio
import pytest
from knarr.dht.plugins import PluginHooks, PluginLoader, PluginContext


class TestPluginHooksDefaults:
    """Default implementations must be safe and non-blocking."""

    def test_on_settlement_review_default_returns_input(self):
        hooks = PluginHooks()
        prepared_tx = {"document_type": "settlement_prepared", "amount": 50.0}
        result = asyncio.get_event_loop().run_until_complete(
            hooks.on_settlement_review(prepared_tx)
        )
        assert result is prepared_tx, "Default should return input unchanged (auto-approve)"

    def test_on_inbound_settlement_default_accepts(self):
        hooks = PluginHooks()
        settle_request = {"amount": 50.0}
        result = asyncio.get_event_loop().run_until_complete(
            hooks.on_inbound_settlement(settle_request)
        )
        assert result is True, "Default should accept"


class TestPluginContextField:
    """query_prepaid_balance must be a field in PluginContext."""

    def test_query_prepaid_balance_field_exists(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(PluginContext)}
        assert "query_prepaid_balance" in fields

    def test_query_prepaid_balance_defaults_to_none(self):
        from pathlib import Path
        from unittest.mock import MagicMock
        ctx = PluginContext(
            node_id="a" * 64,
            plugin_dir=Path("."),
            get_peers=MagicMock(),
            send_to_peer=MagicMock(),
            send_fire_forget=MagicMock(),
            delivery_cb=None,
            log=MagicMock(),
        )
        assert ctx.query_prepaid_balance is None


class TestPluginLoaderSettlementReview:
    """PluginLoader.on_settlement_review: first non-None wins; no plugins = auto-approve."""

    def _make_loader_with_plugins(self, plugins):
        loader = PluginLoader.__new__(PluginLoader)
        loader.plugins = plugins
        return loader

    def test_no_plugins_returns_input(self):
        loader = self._make_loader_with_plugins([])
        prepared_tx = {"document_type": "settlement_prepared"}
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_settlement_review(prepared_tx)
        )
        assert result is prepared_tx

    def test_plugin_returning_none_passes_to_next(self):
        """Plugin returning None = no opinion; next plugin gets a chance (spec: first non-None wins)."""
        class Plugin1(PluginHooks):
            async def on_settlement_review(self, prepared_tx):
                return None  # no opinion, pass to next

        class Plugin2(PluginHooks):
            async def on_settlement_review(self, prepared_tx):
                modified = dict(prepared_tx)
                modified["_approved_by"] = "plugin2"
                return modified

        loader = self._make_loader_with_plugins([Plugin1(), Plugin2()])
        prepared_tx = {"document_type": "settlement_prepared"}
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_settlement_review(prepared_tx)
        )
        # Plugin1 returns None → skipped; Plugin2 returns modified → first non-None wins
        assert result is not None
        assert result["_approved_by"] == "plugin2"

    def test_plugin_error_returns_none_fail_closed(self):
        """Plugin raising an exception should cause fail-closed (None = rejected)."""
        class BrokenPlugin(PluginHooks):
            async def on_settlement_review(self, prepared_tx):
                raise RuntimeError("Plugin exploded")

        loader = self._make_loader_with_plugins([BrokenPlugin()])
        prepared_tx = {"amount": 50.0}
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_settlement_review(prepared_tx)
        )
        assert result is None, "Exception in plugin should fail-closed"

    def test_approving_plugin_returns_modified_doc(self):
        """A plugin that returns a countersigned doc should win."""
        class ApproverPlugin(PluginHooks):
            async def on_settlement_review(self, prepared_tx):
                approved = dict(prepared_tx)
                approved["_countersigned"] = True
                return approved

        loader = self._make_loader_with_plugins([ApproverPlugin()])
        prepared_tx = {"amount": 50.0}
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_settlement_review(prepared_tx)
        )
        assert result is not None
        assert result.get("_countersigned") is True


class TestPluginLoaderInboundSettlement:
    """PluginLoader.on_inbound_settlement: all must agree; any False = reject."""

    def _make_loader_with_plugins(self, plugins):
        loader = PluginLoader.__new__(PluginLoader)
        loader.plugins = plugins
        return loader

    def test_no_plugins_accepts(self):
        loader = self._make_loader_with_plugins([])
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_inbound_settlement({"amount": 50.0})
        )
        assert result is True

    def test_all_accept(self):
        class AcceptPlugin(PluginHooks):
            async def on_inbound_settlement(self, settle_request):
                return True

        loader = self._make_loader_with_plugins([AcceptPlugin(), AcceptPlugin()])
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_inbound_settlement({"amount": 50.0})
        )
        assert result is True

    def test_any_reject_causes_reject(self):
        class AcceptPlugin(PluginHooks):
            async def on_inbound_settlement(self, settle_request):
                return True

        class RejectPlugin(PluginHooks):
            async def on_inbound_settlement(self, settle_request):
                return False

        loader = self._make_loader_with_plugins([AcceptPlugin(), RejectPlugin()])
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_inbound_settlement({"amount": 50.0})
        )
        assert result is False

    def test_plugin_error_rejects_fail_closed(self):
        class BrokenPlugin(PluginHooks):
            async def on_inbound_settlement(self, settle_request):
                raise RuntimeError("Exploded")

        loader = self._make_loader_with_plugins([BrokenPlugin()])
        result = asyncio.get_event_loop().run_until_complete(
            loader.on_inbound_settlement({"amount": 50.0})
        )
        assert result is False, "Exception in plugin should fail-closed (reject)"
