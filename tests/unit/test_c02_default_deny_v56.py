import importlib.util
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_HANDLER_PATH = (
    Path(__file__).parent.parent.parent
    / "src"
    / "knarr"
    / "plugins"
    / "08-punchhole-frontend"
    / "handler.py"
)
_SPEC = importlib.util.spec_from_file_location("punchhole_frontend_handler_v56", _HANDLER_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
PunchholeFrontendPlugin = _MODULE.PunchholeFrontendPlugin


def _make_plugin():
    ctx = MagicMock()
    ctx.node_id = "b" * 64
    ctx.state_dir = Path(tempfile.mkdtemp())
    ctx.plugin_dir = ctx.state_dir
    ctx.subscribe_events = None
    ctx.emit_event = MagicMock()
    ctx.send_fire_forget = None
    ctx.get_plugin = None
    ctx._node = MagicMock()
    ctx._node.storage = MagicMock()
    ctx._node.storage.get_pubkey_by_node_id.return_value = None

    plugin = PunchholeFrontendPlugin.__new__(PunchholeFrontendPlugin)
    plugin._ctx = ctx
    plugin._config = {}
    plugin._debug = False
    plugin._cache = {}
    plugin._acl = {}
    plugin._backend_ready = True
    plugin._db_path = str(ctx.state_dir / "disclosure.db")
    conn = sqlite3.connect(plugin._db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS disclosure_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT,
            object_key TEXT,
            acl_group TEXT,
            outcome TEXT,
            ts REAL
        )
        """
    )
    conn.commit()
    conn.close()
    return plugin


@pytest.mark.asyncio
async def test_unknown_requester_is_rejected_by_default(caplog):
    plugin = _make_plugin()
    requester = "a" * 64

    with caplog.at_level("INFO", logger="knarr.plugin.punchhole-frontend"):
        result = await plugin._process_request(
            requester_node_id=requester,
            object_key="skills",
            signed_request={},
            trace_id="trace-1",
        )

    assert result == {"status": "rejected", "error": "access_denied", "object_key": "skills"}
    assert (
        f"PUNCHHOLE_ACL_DENY node_id={requester} reason=unknown_and_no_public_signed_tier"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_unknown_requester_denial_log_is_single_info_record(caplog):
    plugin = _make_plugin()
    requester = "c" * 64

    with caplog.at_level("INFO", logger="knarr.plugin.punchhole-frontend"):
        await plugin._process_request(
            requester_node_id=requester,
            object_key="skills",
            signed_request={},
            trace_id="trace-1b",
        )

    deny_records = [record for record in caplog.records if record.getMessage().startswith("PUNCHHOLE_ACL_DENY")]
    assert len(deny_records) == 1
    assert deny_records[0].levelname == "INFO"
    assert (
        deny_records[0].getMessage()
        == f"PUNCHHOLE_ACL_DENY node_id={requester} reason=unknown_and_no_public_signed_tier"
    )


@pytest.mark.asyncio
async def test_explicit_public_signed_entry_preserves_access():
    plugin = _make_plugin()
    requester = "a" * 64
    plugin._acl[requester] = "public_signed"

    result = await plugin._process_request(
        requester_node_id=requester,
        object_key="skills",
        signed_request={},
        trace_id="trace-2",
    )

    assert result["status"] == "miss"
    assert result["acl_group"] == "public_signed"


def test_release_notes_call_out_default_deny_cutover():
    release_notes = (
        Path(__file__).parent.parent.parent / "docs" / "releases" / "v0.56.0.md"
    ).read_text(encoding="utf-8")

    # Forseti's rewrite uses "BREAKING CHANGE" (all caps) — case-insensitive check
    assert "breaking change" in release_notes.lower()
    assert "default-deny" in release_notes
    # v0.56.0 release notes use the actual config schema (trusted_nodes)
    # rather than the brief's invented "public_signed" tier name.
    assert "trusted_nodes" in release_notes


def test_release_notes_include_acl_migration_snippet():
    release_notes = (
        Path(__file__).parent.parent.parent / "docs" / "releases" / "v0.56.0.md"
    ).read_text(encoding="utf-8")

    # The migration snippet uses trusted_nodes (the actual ACL escape hatch)
    # rather than the brief's [exposure.acl] / public_signed naming.
    assert "trusted_nodes" in release_notes
