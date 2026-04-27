import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from knarr.core.models import NodeInfo
from knarr.dht.storage import Storage


BASE_DIR = Path(__file__).resolve().parents[2]
PLUGIN_PATH = BASE_DIR / "src" / "knarr" / "plugins" / "09-punchhole-backend" / "handler.py"
spec = importlib.util.spec_from_file_location("punchhole_backend_b03_v58", PLUGIN_PATH)
punchhole = importlib.util.module_from_spec(spec)
sys.modules["punchhole_backend_b03_v58"] = punchhole
spec.loader.exec_module(punchhole)


NODE_ID = "a" * 64


def _columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {row[1]: row for row in conn.execute("PRAGMA table_info(peers)").fetchall()}
    finally:
        conn.close()


def _failed_count(db_path, node_id=NODE_ID):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT failed_count FROM peers WHERE node_id = ?",
            (node_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _plugin(db_path):
    plugin = punchhole.PunchholeBackendPlugin.__new__(punchhole.PunchholeBackendPlugin)
    plugin._ctx = SimpleNamespace(storage_path=str(db_path))
    plugin._debug = False
    return plugin


def test_fresh_database_has_failed_count_default_zero(tmp_path):
    db_path = tmp_path / "node.db"
    storage = Storage(str(db_path))
    try:
        storage.upsert_peer(NodeInfo(NODE_ID, "127.0.0.1", 9000))
    finally:
        storage.close()

    columns = _columns(db_path)
    assert "failed_count" in columns
    assert _failed_count(db_path) == 0


def test_pre_migration_database_adds_failed_count_without_data_loss(tmp_path):
    db_path = tmp_path / "node.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE peers (
            node_id TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            last_seen REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO peers (node_id, host, port, last_seen) VALUES (?, ?, ?, ?)",
        (NODE_ID, "10.0.0.2", 9001, 123.0),
    )
    conn.commit()
    conn.close()

    storage = Storage(str(db_path))
    storage.close()

    assert "failed_count" in _columns(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT host, port, failed_count FROM peers WHERE node_id = ?",
            (NODE_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("10.0.0.2", 9001, 0)


def test_failed_outbound_increments_counter(tmp_path):
    db_path = tmp_path / "node.db"
    storage = Storage(str(db_path))
    try:
        storage.upsert_peer(NodeInfo(NODE_ID, "127.0.0.1", 9000))
    finally:
        storage.close()

    plugin = _plugin(db_path)
    plugin.record_outbound_failure(NODE_ID)
    plugin.record_outbound_failure(NODE_ID)

    assert _failed_count(db_path) == 2


def test_successful_outbound_resets_counter(tmp_path):
    db_path = tmp_path / "node.db"
    storage = Storage(str(db_path))
    try:
        storage.upsert_peer(NodeInfo(NODE_ID, "127.0.0.1", 9000))
    finally:
        storage.close()

    plugin = _plugin(db_path)
    plugin.record_outbound_failure(NODE_ID)
    plugin.record_outbound_success(NODE_ID)

    assert _failed_count(db_path) == 0


def test_card_consumer_receives_populated_failed_count(tmp_path):
    db_path = tmp_path / "node.db"
    storage = Storage(str(db_path))
    try:
        storage.upsert_peer(NodeInfo(NODE_ID, "127.0.0.1", 9000))
    finally:
        storage.close()
    plugin = _plugin(db_path)
    plugin.record_outbound_failure(NODE_ID)

    status = plugin._read_peers_status()

    assert status["count"] == 1
    assert status["peers"][0]["node_id"] == NODE_ID
    assert status["peers"][0]["failed_count"] == 1
