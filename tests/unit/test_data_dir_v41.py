import os
from pathlib import Path

import pytest

from knarr.cli.main import _resolve_data_dir, _warn_duplicate_identity_files
from knarr.dht.node import DHTNode
from knarr.dht.plugins import PluginLoader


def test_data_dir_from_cli_flag_overrides_env_config_and_default(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("KNARR_DATA_DIR", str(tmp_path / "env"))

    resolved, explicit = _resolve_data_dir(
        str(tmp_path / "cli"),
        {"node": {"data_dir": "config-state"}},
        str(config_dir),
    )

    assert resolved == str((tmp_path / "cli").resolve())
    assert explicit is True


def test_data_dir_from_env_used_when_no_cli_flag(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("KNARR_DATA_DIR", str(tmp_path / "env"))

    resolved, explicit = _resolve_data_dir(
        None,
        {"node": {"data_dir": "config-state"}},
        str(config_dir),
    )

    assert resolved == str((tmp_path / "env").resolve())
    assert explicit is True


def test_data_dir_from_config_used_when_no_env_var(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.delenv("KNARR_DATA_DIR", raising=False)

    resolved, explicit = _resolve_data_dir(
        None,
        {"node": {"data_dir": "state"}},
        str(config_dir),
    )

    assert resolved == str((config_dir / "state").resolve())
    assert explicit is True


def test_data_dir_defaults_to_config_dir_when_unset(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.delenv("KNARR_DATA_DIR", raising=False)

    resolved, explicit = _resolve_data_dir(None, {"node": {}}, str(config_dir))

    assert resolved == str(config_dir.resolve())
    assert explicit is False


def test_data_dir_priority_order_cli_env_config_default(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("KNARR_DATA_DIR", str(tmp_path / "env"))
    config = {"node": {"data_dir": "state"}}

    cli_resolved, _ = _resolve_data_dir(str(tmp_path / "cli"), config, str(config_dir))
    env_resolved, _ = _resolve_data_dir(None, config, str(config_dir))
    monkeypatch.delenv("KNARR_DATA_DIR", raising=False)
    cfg_resolved, _ = _resolve_data_dir(None, config, str(config_dir))
    default_resolved, _ = _resolve_data_dir(None, {"node": {}}, str(config_dir))

    assert cli_resolved == str((tmp_path / "cli").resolve())
    assert env_resolved == str((tmp_path / "env").resolve())
    assert cfg_resolved == str((config_dir / "state").resolve())
    assert default_resolved == str(config_dir.resolve())


@pytest.mark.asyncio
async def test_first_boot_creates_directory_structure_under_data_dir(tmp_path):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "state"
    config_dir.mkdir()
    data_dir.mkdir()

    node = DHTNode(
        "127.0.0.1",
        0,
        storage_path=str(data_dir / "node.db"),
        config={
            "_config_dir": str(config_dir),
            "_data_dir": str(data_dir),
            "_data_dir_explicit": True,
            "node": {"sidecar_port": 0},
        },
    )

    await node.start()
    await node.stop()

    assert (data_dir / "node.db").exists()
    assert (data_dir / "vault.db").exists()
    assert (data_dir / "cert.pem").exists()
    assert (data_dir / "key.pem").exists()


def test_warning_logged_when_identity_files_exist_in_both_locations(caplog, tmp_path):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "state"
    config_dir.mkdir()
    data_dir.mkdir()

    for parent in (config_dir, data_dir):
        (parent / "key.pem").write_text("key")
        (parent / "cert.pem").write_text("cert")

    _warn_duplicate_identity_files(str(config_dir), str(data_dir))

    assert "Identity files found in both config_dir and data_dir" in caplog.text


def test_plugin_state_writes_to_plugin_dir_when_data_dir_is_unset(tmp_path):
    config_dir = tmp_path / "config"
    plugin_dir = config_dir / "plugins" / "demo-plugin"
    plugin_dir.mkdir(parents=True)

    (plugin_dir / "plugin.toml").write_text(
        'name = "demo-plugin"\nversion = "1.0.0"\nhandler = "handler:DemoPlugin"\n'
    )
    (plugin_dir / "handler.py").write_text(
        "from knarr.dht.plugins import PluginHooks\n"
        "class DemoPlugin(PluginHooks):\n"
        "    def __init__(self, ctx, config=None):\n"
        "        self._ctx = ctx\n"
        "        (ctx.state_dir / 'state.txt').write_text('ok')\n"
    )

    loader = PluginLoader(
        config_dir=Path(config_dir),
        get_peers_cb=lambda: [],
        send_to_peer_cb=lambda *_args, **_kwargs: None,
        node_id="a" * 64,
    )
    loader.load_plugins()

    assert (plugin_dir / "state.txt").exists()
    # Filter out package-fallback plugins (tor, bcw, punchhole-*) — we only
    # care about the fixture's demo-plugin.
    demo = next(p for p in loader.plugins if p.__class__.__name__ == "DemoPlugin")
    assert demo._ctx.state_dir == plugin_dir
