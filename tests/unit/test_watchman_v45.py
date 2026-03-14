"""Watchman contract tests — v0.45.0 retroactive coverage.

Watchman (knarr.watchman.*) shipped in v0.45.0 with zero test coverage.
These tests cover the pure/mockable logic in all five modules:
  - config.py:         _deep_merge, load_config defaults
  - upgrader.py:       _parse_source, _version_tuple, check_available (no update)
  - supervisor.py:     get_status (no proc), backoff formula, give-up threshold
  - plugin_manager.py: _parse_constraint, _satisfies, load_manifest, _write_manifest_simple

Network I/O (GitHub API, pip) and subprocess spawning are NOT exercised here.
Those paths require integration tests (planned for v0.46.0).
"""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------

def test_deep_merge_basic():
    from knarr.watchman.config import _deep_merge
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    override = {"b": {"c": 99}, "e": 5}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}


def test_deep_merge_does_not_mutate_base():
    from knarr.watchman.config import _deep_merge
    base = {"a": {"x": 1}}
    _deep_merge(base, {"a": {"x": 2}})
    assert base["a"]["x"] == 1  # base unchanged


def test_deep_merge_new_nested_key_in_override():
    """Override can add a new nested key that doesn't exist in base."""
    from knarr.watchman.config import _deep_merge
    base = {"a": {"x": 1}}
    result = _deep_merge(base, {"a": {"y": 2}})
    assert result == {"a": {"x": 1, "y": 2}}


def test_deep_merge_override_wins_on_scalar():
    from knarr.watchman.config import _deep_merge
    result = _deep_merge({"k": "old"}, {"k": "new"})
    assert result["k"] == "new"


def test_load_config_returns_defaults_when_no_file():
    """load_config must return sane defaults when watchman.toml does not exist."""
    from knarr.watchman.config import load_config
    cfg = load_config("/nonexistent/path/watchman.toml")

    # Node defaults
    assert cfg["node"]["command"] == "knarr"
    assert cfg["node"]["args"] == ["serve"]

    # Health defaults
    assert cfg["health"]["health_interval"] == 10
    assert cfg["health"]["health_fail_threshold"] == 3
    assert cfg["health"]["cockpit_url"] == "http://127.0.0.1:8080"

    # Recovery defaults
    assert cfg["recovery"]["max_restarts"] == 10
    assert cfg["recovery"]["initial_backoff"] == 5
    assert cfg["recovery"]["max_backoff"] == 300
    assert cfg["recovery"]["backoff_reset_uptime"] == 1800

    # Upgrade defaults
    assert cfg["upgrade"]["auto_upgrade"] is False
    assert cfg["upgrade"]["source"] == "github:knarrnet/knarr"
    assert cfg["upgrade"]["drain_timeout"] == 60
    assert cfg["upgrade"]["health_timeout"] == 30

    # Plugin defaults
    assert cfg["plugins"]["sync_plugins"] is False


def test_load_config_merges_toml_override(tmp_path):
    """load_config merges file values over defaults without losing unset keys."""
    toml_file = tmp_path / "watchman.toml"
    toml_file.write_bytes(
        b"[health]\nhealth_interval = 30\nhealth_fail_threshold = 5\n"
        b"cockpit_url = \"http://127.0.0.1:9000\"\n"
    )
    from knarr.watchman.config import load_config
    cfg = load_config(str(toml_file))

    # Overridden values
    assert cfg["health"]["health_interval"] == 30
    assert cfg["health"]["health_fail_threshold"] == 5
    assert cfg["health"]["cockpit_url"] == "http://127.0.0.1:9000"

    # Unset values still come from defaults
    assert cfg["recovery"]["max_restarts"] == 10
    assert cfg["upgrade"]["auto_upgrade"] is False


# ---------------------------------------------------------------------------
# upgrader.py
# ---------------------------------------------------------------------------

def test_parse_source_valid():
    from knarr.watchman.upgrader import _parse_source
    org, repo = _parse_source("github:knarrnet/knarr")
    assert org == "knarrnet"
    assert repo == "knarr"


def test_parse_source_invalid_scheme():
    from knarr.watchman.upgrader import _parse_source
    with pytest.raises(ValueError, match="Unsupported"):
        _parse_source("pypi:knarr")


def test_parse_source_invalid_slug():
    from knarr.watchman.upgrader import _parse_source
    with pytest.raises(ValueError, match="Invalid"):
        _parse_source("github:knarrnet")  # missing /repo


def test_version_tuple_normal():
    from knarr.watchman.upgrader import _version_tuple
    assert _version_tuple("0.45.0") == (0, 45, 0)
    assert _version_tuple("v1.2.3") == (1, 2, 3)
    assert _version_tuple("10.0.1") == (10, 0, 1)


def test_version_tuple_bad_input():
    from knarr.watchman.upgrader import _version_tuple
    assert _version_tuple("bad") == (0,)
    assert _version_tuple("") == (0,)


def test_version_tuple_non_standard_lengths():
    """Two-part and four-part versions must compare consistently."""
    from knarr.watchman.upgrader import _version_tuple
    assert _version_tuple("1.0") == (1, 0)
    assert _version_tuple("1.0.0.1") == (1, 0, 0, 1)
    # Two-part is less than three-part equivalent
    assert _version_tuple("1.0") < _version_tuple("1.0.0")


def test_check_available_returns_none_when_current(monkeypatch):
    """check_available must return None when running version matches latest."""
    from knarr.watchman import upgrader as upg

    monkeypatch.setattr(upg, "_fetch_latest_release", lambda org, repo: {"tag_name": "v0.45.0"})
    monkeypatch.setattr(upg, "_get_running_version", lambda: "0.45.0")

    cfg = {
        "node": {"command": "knarr", "args": ["serve"], "data_dir": "."},
        "health": {"health_interval": 10, "health_fail_threshold": 3, "cockpit_url": "http://127.0.0.1:8080"},
        "recovery": {"max_restarts": 10, "initial_backoff": 5, "max_backoff": 300, "backoff_reset_uptime": 1800},
        "upgrade": {
            "auto_upgrade": False, "check_interval": 3600,
            "drain_timeout": 60, "health_timeout": 30,
            "source": "github:knarrnet/knarr",
        },
        "plugins": {"sync_plugins": False, "plugin_dir": "plugins"},
    }
    upgrader = upg.Upgrader(cfg, MagicMock())
    result = upgrader.check_available()
    assert result is None


def test_check_available_returns_tag_when_newer(monkeypatch):
    """check_available must return the new tag when a newer version is available."""
    from knarr.watchman import upgrader as upg

    monkeypatch.setattr(upg, "_fetch_latest_release", lambda org, repo: {"tag_name": "v0.46.0"})
    monkeypatch.setattr(upg, "_get_running_version", lambda: "0.45.0")

    cfg = {
        "node": {"command": "knarr", "args": ["serve"], "data_dir": "."},
        "health": {"health_interval": 10, "health_fail_threshold": 3, "cockpit_url": "http://127.0.0.1:8080"},
        "recovery": {"max_restarts": 10, "initial_backoff": 5, "max_backoff": 300, "backoff_reset_uptime": 1800},
        "upgrade": {
            "auto_upgrade": False, "check_interval": 3600,
            "drain_timeout": 60, "health_timeout": 30,
            "source": "github:knarrnet/knarr",
        },
        "plugins": {"sync_plugins": False, "plugin_dir": "plugins"},
    }
    upgrader = upg.Upgrader(cfg, MagicMock())
    result = upgrader.check_available()
    assert result == "v0.46.0"


def test_check_available_missing_tag_name(monkeypatch):
    """check_available must return None when release payload has no tag_name."""
    from knarr.watchman import upgrader as upg

    monkeypatch.setattr(upg, "_fetch_latest_release", lambda org, repo: {})  # no tag_name
    monkeypatch.setattr(upg, "_get_running_version", lambda: "0.45.0")

    cfg = {
        "node": {"command": "knarr", "args": ["serve"], "data_dir": "."},
        "health": {"health_interval": 10, "health_fail_threshold": 3, "cockpit_url": "http://127.0.0.1:8080"},
        "recovery": {"max_restarts": 10, "initial_backoff": 5, "max_backoff": 300, "backoff_reset_uptime": 1800},
        "upgrade": {"auto_upgrade": False, "check_interval": 3600, "drain_timeout": 60, "health_timeout": 30, "source": "github:knarrnet/knarr"},
        "plugins": {"sync_plugins": False, "plugin_dir": "plugins"},
    }
    upgrader = upg.Upgrader(cfg, MagicMock())
    result = upgrader.check_available()
    assert result is None


def test_check_available_returns_none_on_api_failure(monkeypatch):
    """check_available must return None when GitHub API is unavailable."""
    from knarr.watchman import upgrader as upg

    monkeypatch.setattr(upg, "_fetch_latest_release", lambda org, repo: None)
    monkeypatch.setattr(upg, "_get_running_version", lambda: "0.45.0")

    cfg = {
        "node": {"command": "knarr", "args": ["serve"], "data_dir": "."},
        "health": {"health_interval": 10, "health_fail_threshold": 3, "cockpit_url": "http://127.0.0.1:8080"},
        "recovery": {"max_restarts": 10, "initial_backoff": 5, "max_backoff": 300, "backoff_reset_uptime": 1800},
        "upgrade": {
            "auto_upgrade": False, "check_interval": 3600,
            "drain_timeout": 60, "health_timeout": 30,
            "source": "github:knarrnet/knarr",
        },
        "plugins": {"sync_plugins": False, "plugin_dir": "plugins"},
    }
    upgrader = upg.Upgrader(cfg, MagicMock())
    result = upgrader.check_available()
    assert result is None


def test_sha256_file_roundtrip(tmp_path):
    import hashlib
    from knarr.watchman.upgrader import _sha256_file
    data = b"knarr watchman sha256 test"
    f = tmp_path / "test.bin"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert _sha256_file(str(f)) == expected


# ---------------------------------------------------------------------------
# supervisor.py
# ---------------------------------------------------------------------------

def _make_supervisor_cfg():
    return {
        "node": {"command": "knarr", "args": ["serve"], "data_dir": "."},
        "health": {
            "health_interval": 10,
            "health_fail_threshold": 3,
            "cockpit_url": "http://127.0.0.1:8080",
        },
        "recovery": {
            "max_restarts": 10,
            "initial_backoff": 5,
            "max_backoff": 300,
            "backoff_reset_uptime": 1800,
        },
        "upgrade": {
            "auto_upgrade": False, "check_interval": 3600,
            "drain_timeout": 60, "health_timeout": 30,
            "source": "github:knarrnet/knarr",
        },
        "plugins": {"sync_plugins": False, "plugin_dir": "plugins"},
    }


def test_supervisor_get_status_no_proc():
    """get_status must return sane defaults when node has not been spawned."""
    from knarr.watchman.supervisor import Supervisor
    s = Supervisor(_make_supervisor_cfg())
    status = s.get_status()
    assert status["node_running"] is False
    assert status["node_pid"] is None
    assert status["restart_count"] == 0
    assert status["health_fails"] == 0
    assert isinstance(status["uptime_seconds"], int)


@pytest.mark.asyncio
async def test_supervisor_backoff_increases_exponentially():
    """
    _restart backoff must double each attempt and cap at max_backoff.
    Verified by capturing actual sleep durations from Supervisor._restart().
    """
    import time
    from knarr.watchman.supervisor import Supervisor

    cfg = _make_supervisor_cfg()
    cfg["recovery"]["initial_backoff"] = 5
    cfg["recovery"]["max_backoff"] = 300
    cfg["recovery"]["max_restarts"] = 20

    s = Supervisor(cfg)
    s._running = True
    s._start_time = time.monotonic()  # uptime ≈ 0 → no backoff reset

    sleep_durations = []

    async def mock_sleep(secs):
        sleep_durations.append(secs)

    async def mock_spawn():
        pass

    s._spawn = mock_spawn

    with patch("knarr.watchman.supervisor.asyncio.sleep", side_effect=mock_sleep):
        for _ in range(7):
            await s._restart()

    assert sleep_durations == [5, 10, 20, 40, 80, 160, 300], (
        f"Backoff sequence wrong: {sleep_durations}. "
        "Expected [5, 10, 20, 40, 80, 160, 300] (doubles each attempt, capped at 300)."
    )


@pytest.mark.asyncio
async def test_supervisor_restart_gives_up_when_max_restarts_zero():
    """max_restarts=0 must give up on the very first crash (no restarts allowed)."""
    import time
    from knarr.watchman.supervisor import Supervisor

    cfg = _make_supervisor_cfg()
    cfg["recovery"]["max_restarts"] = 0

    s = Supervisor(cfg)
    s._running = True
    s._restart_count = 0
    s._start_time = time.monotonic()

    spawn_calls = []
    async def mock_spawn():
        spawn_calls.append(1)
    s._spawn = mock_spawn

    with patch("knarr.watchman.supervisor.asyncio.sleep", return_value=None):
        await s._restart()

    assert s._running is False, "max_restarts=0 must give up immediately"
    assert len(spawn_calls) == 0


@pytest.mark.asyncio
async def test_supervisor_restart_gives_up_at_max_restarts():
    """
    _restart must stop the supervisor loop (set _running=False) after max_restarts
    attempts without a backoff reset, and NOT call _spawn.
    """
    import time
    from knarr.watchman.supervisor import Supervisor
    import asyncio

    cfg = _make_supervisor_cfg()
    cfg["recovery"]["max_restarts"] = 3

    s = Supervisor(cfg)
    s._running = True
    s._restart_count = 3              # already at max
    s._start_time = time.monotonic()  # uptime ≈ 0 → well below backoff_reset_uptime=1800 → no reset

    spawn_calls = []

    async def mock_spawn():
        spawn_calls.append(1)

    s._spawn = mock_spawn

    # Patch asyncio.sleep to avoid waiting
    with patch("knarr.watchman.supervisor.asyncio.sleep", return_value=None):
        await s._restart()

    assert s._running is False, "_running must be False after WATCHMAN_GIVE_UP"
    assert len(spawn_calls) == 0, "_spawn must NOT be called after give-up"


@pytest.mark.asyncio
async def test_supervisor_restart_resets_count_on_long_uptime():
    """
    If uptime >= backoff_reset_uptime at the time of restart, restart_count
    must be reset to 0 before incrementing (so restart_count ends up at 1).
    """
    from knarr.watchman.supervisor import Supervisor
    import time

    cfg = _make_supervisor_cfg()
    cfg["recovery"]["max_restarts"] = 10
    cfg["recovery"]["backoff_reset_uptime"] = 60  # 60s threshold

    s = Supervisor(cfg)
    s._running = True
    s._restart_count = 5
    s._start_time = time.monotonic() - 90  # 90s uptime → triggers reset

    spawn_calls = []

    async def mock_spawn():
        spawn_calls.append(1)

    s._spawn = mock_spawn

    with patch("knarr.watchman.supervisor.asyncio.sleep", return_value=None):
        await s._restart()

    assert s._restart_count == 1, (
        f"restart_count={s._restart_count}; expected 1 after backoff reset "
        "(reset to 0 then incremented by 1)"
    )
    assert len(spawn_calls) == 1, "_spawn must be called after successful reset+restart"


# ---------------------------------------------------------------------------
# plugin_manager.py
# ---------------------------------------------------------------------------

def test_parse_constraint_gte():
    from knarr.watchman.plugin_manager import _parse_constraint
    op, ver = _parse_constraint(">=1.0.0")
    assert op == ">="
    assert ver == "1.0.0"


def test_parse_constraint_exact_with_v():
    from knarr.watchman.plugin_manager import _parse_constraint
    op, ver = _parse_constraint("v1.2.3")
    assert op == "=="
    assert ver == "1.2.3"


def test_parse_constraint_equality():
    from knarr.watchman.plugin_manager import _parse_constraint
    op, ver = _parse_constraint("==2.0.0")
    assert op == "=="
    assert ver == "2.0.0"


def test_satisfies_gte():
    from knarr.watchman.plugin_manager import _satisfies
    assert _satisfies((1, 1, 0), ">=", (1, 0, 0)) is True
    assert _satisfies((0, 9, 0), ">=", (1, 0, 0)) is False
    assert _satisfies((1, 0, 0), ">=", (1, 0, 0)) is True


def test_satisfies_equality():
    from knarr.watchman.plugin_manager import _satisfies
    assert _satisfies((1, 2, 3), "==", (1, 2, 3)) is True
    assert _satisfies((1, 2, 4), "==", (1, 2, 3)) is False


def test_satisfies_not_equal():
    from knarr.watchman.plugin_manager import _satisfies
    assert _satisfies((1, 2, 3), "!=", (1, 2, 4)) is True
    assert _satisfies((1, 2, 3), "!=", (1, 2, 3)) is False


def test_load_manifest_empty_when_no_file(tmp_path):
    from knarr.watchman.plugin_manager import load_manifest
    result = load_manifest(str(tmp_path / "nonexistent.toml"))
    assert result == {"plugins": {}}


def test_write_manifest_simple_special_chars_in_source(tmp_path):
    """Sources with special chars (slashes, colons, dots) must survive the roundtrip."""
    from knarr.watchman.plugin_manager import _write_manifest_simple, load_manifest

    manifest = {
        "plugins": {
            "my-plugin": {
                "source": "github:org/repo-with.dots",
                "version": ">=1.0.0",
                "enabled": True,
            }
        }
    }
    path = str(tmp_path / "plugins.toml")
    _write_manifest_simple(path, manifest)
    result = load_manifest(path)
    assert result["plugins"]["my-plugin"]["source"] == "github:org/repo-with.dots"


def test_write_manifest_simple_roundtrip(tmp_path):
    """_write_manifest_simple must produce a file that load_manifest can read back."""
    from knarr.watchman.plugin_manager import _write_manifest_simple, load_manifest

    manifest = {
        "plugins": {
            "echo": {
                "source": "github:knarrnet/knarr-echo",
                "version": ">=1.0.0",
                "enabled": True,
            },
            "firewall": {
                "source": "file:///local/plugins/firewall",
                "enabled": False,
            },
        }
    }

    path = str(tmp_path / "plugins.toml")
    _write_manifest_simple(path, manifest)

    # File must exist and be non-empty
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0

    # Content must be readable back via load_manifest
    result = load_manifest(path)
    assert "plugins" in result
    assert "echo" in result["plugins"]
    assert result["plugins"]["echo"]["enabled"] is True
    assert result["plugins"]["firewall"]["enabled"] is False


def test_plugin_manager_no_crash_on_empty_manifest(tmp_path):
    """PluginManager.sync() must not crash when no plugins are declared."""
    from knarr.watchman.plugin_manager import PluginManager

    cfg = {
        "node": {"command": "knarr", "args": ["serve"], "data_dir": str(tmp_path)},
        "health": {"health_interval": 10, "health_fail_threshold": 3, "cockpit_url": "http://127.0.0.1:8080"},
        "recovery": {"max_restarts": 10, "initial_backoff": 5, "max_backoff": 300, "backoff_reset_uptime": 1800},
        "upgrade": {"auto_upgrade": False, "check_interval": 3600, "drain_timeout": 60, "health_timeout": 30, "source": "github:knarrnet/knarr"},
        "plugins": {"sync_plugins": False, "plugin_dir": "plugins"},
    }
    pm = PluginManager(cfg)
    pm.sync()  # must not raise


def test_get_installed_version_returns_none_when_not_present(tmp_path):
    """get_installed_version must return None when plugin directory doesn't exist."""
    from knarr.watchman.plugin_manager import get_installed_version
    result = get_installed_version("echo", str(tmp_path / "plugins"))
    assert result is None
