"""KAD-04: SQLite persistence for routing table and provider cache."""
import sys
import os
import time
import tempfile
import sqlite3
import hashlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '01-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from kbuckets import KBucketTable
from providers import ProviderCache


LOCAL_ID = "0" * 64


def _tmp_db():
    """Return a unique temp path for a SQLite DB (not opened — avoids Windows lock)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # delete the empty file so sqlite3 creates it fresh
    return path


def _safe_unlink(path):
    """Best-effort delete on Windows (DB may still be locked briefly)."""
    try:
        os.unlink(path)
    except OSError:
        pass


# ---- routing table tests ----

def test_routing_table_survives_restart():
    """Routing table peers written to DB must be loadable after reinit."""
    db_path = _tmp_db()
    try:
        # Session 1: add peers, flush
        table1 = KBucketTable(LOCAL_ID, k=8, db_path=db_path)
        peer_id = format(1, '064x')
        table1.add_peer(peer_id, "10.0.0.1", 9001)
        table1.save_on_shutdown()
        del table1  # release connections

        # Session 2: reload
        table2 = KBucketTable(LOCAL_ID, k=8, db_path=db_path)
        closest = table2.get_closest(peer_id, count=8)
        found = {p["node_id"] for p in closest}
        del table2

        assert peer_id in found, "Peer must survive a routing-table restart"
    finally:
        _safe_unlink(db_path)


def test_routing_table_multiple_peers_survive_restart():
    """Multiple peers survive shutdown/restart cycle."""
    db_path = _tmp_db()
    try:
        peer_ids = [format(32 + i, '064x') for i in range(5)]

        table1 = KBucketTable(LOCAL_ID, k=8, db_path=db_path)
        for i, pid in enumerate(peer_ids):
            table1.add_peer(pid, f"10.0.0.{i + 1}", 9000 + i)
        table1.save_on_shutdown()
        del table1

        table2 = KBucketTable(LOCAL_ID, k=8, db_path=db_path)
        found = set()
        for bucket in table2.buckets:
            for peer in bucket:
                found.add(peer[0])
        del table2

        for pid in peer_ids:
            assert pid in found, f"Peer {pid[:8]}... must survive restart"
    finally:
        _safe_unlink(db_path)


# ---- provider cache tests ----

def test_provider_cache_survives_restart():
    """Provider records must be loadable after reinit."""
    db_path = _tmp_db()
    try:
        cache1 = ProviderCache(max_records=100, db_path=db_path)
        cache1.store("my-skill", "a" * 64, "10.0.0.1", 9001, 9002, ttl=3600)
        del cache1

        # Reinit — should load from DB
        cache2 = ProviderCache(max_records=100, db_path=db_path)
        results = cache2.get_providers("my-skill")
        del cache2

        assert len(results) == 1, "Provider record must survive restart"
        assert results[0]["node_id"] == "a" * 64
    finally:
        _safe_unlink(db_path)


def test_provider_cache_expired_records_filtered_on_load():
    """Expired records must NOT be loaded on init (TTL check on load)."""
    db_path = _tmp_db()
    try:
        # Create the schema and pre-populate with an expired record directly
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kad_providers ("
            "  skill_key_hash TEXT, node_id TEXT, host TEXT, port INTEGER,"
            "  sidecar_port INTEGER, stored_at REAL, ttl INTEGER, skill_key TEXT,"
            "  PRIMARY KEY (skill_key_hash, node_id)"
            ")"
        )
        key_hash = hashlib.sha256(b"old-skill").hexdigest()
        # stored_at = 7200 seconds ago, ttl = 3600 — clearly expired
        old_stored_at = time.time() - 7200
        conn.execute(
            "INSERT INTO kad_providers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key_hash, "b" * 64, "10.0.0.2", 9003, 0, old_stored_at, 3600, "old-skill")
        )
        conn.commit()
        conn.close()

        # Load — expired record must be filtered out
        cache = ProviderCache(max_records=100, db_path=db_path)
        results = cache.get_providers("old-skill")
        del cache

        assert results == [], "Expired records must not be loaded on init"
    finally:
        _safe_unlink(db_path)


def test_provider_cache_remove_deletes_from_db():
    """Removing a provider must also delete it from the DB."""
    db_path = _tmp_db()
    try:
        cache = ProviderCache(max_records=100, db_path=db_path)
        node_id = "c" * 64
        cache.store("remove-test", node_id, "10.0.0.3", 9004, 0, ttl=3600)
        cache.remove("remove-test", node_id)
        del cache

        # Reload to confirm DB is also empty
        cache2 = ProviderCache(max_records=100, db_path=db_path)
        results = cache2.get_providers("remove-test")
        del cache2

        assert results == [], "Removed record must not appear after restart"
    finally:
        _safe_unlink(db_path)


def test_no_db_path_in_memory_only():
    """Without db_path, cache works purely in-memory (no crash)."""
    cache = ProviderCache(max_records=100, db_path=None)
    cache.store("in-mem-skill", "d" * 64, "127.0.0.1", 9000, 0, ttl=3600)
    results = cache.get_providers("in-mem-skill")
    assert len(results) == 1
