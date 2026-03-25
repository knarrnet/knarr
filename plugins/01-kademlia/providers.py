import hashlib
import heapq
import sqlite3
import time
from typing import List, Dict, Any, Optional

class ProviderCache:
    """In-memory cache for skill providers.

    KAD-04: Supports optional SQLite write-through persistence. Pass db_path to enable.
    Schema: kad_providers (skill_key_hash TEXT, node_id TEXT, host TEXT, port INT,
                           sidecar_port INT, stored_at REAL, ttl INT, skill_key TEXT,
                           PRIMARY KEY (skill_key_hash, node_id))
    """

    def __init__(self, max_records: int = 50000, db_path: Optional[str] = None):
        self.max_records = max_records
        # sha256(skill_key) -> {node_id -> {host, port, sidecar_port, stored_at, ttl, skill_key}}
        self.cache: Dict[str, Dict[str, Dict]] = {}
        self._total_records = 0
        self._db_path = db_path
        # KAD-08: min-heap for O(log N) eviction, indexed by (stored_at, skill_key_hash, node_id)
        self._evict_heap: list = []  # heap of (stored_at, skill_key_hash, node_id)

        if self._db_path:
            self._init_db()
            self._load_from_db()

    def _get_key(self, skill_key: str) -> str:
        return hashlib.sha256(skill_key.encode()).hexdigest()

    # ---- KAD-04: SQLite persistence ----------------------------------------

    def _init_db(self):
        """Create the provider cache schema if it doesn't exist."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kad_providers ("
                "  skill_key_hash TEXT,"
                "  node_id TEXT,"
                "  host TEXT,"
                "  port INTEGER,"
                "  sidecar_port INTEGER,"
                "  stored_at REAL,"
                "  ttl INTEGER,"
                "  skill_key TEXT,"
                "  PRIMARY KEY (skill_key_hash, node_id)"
                ")"
            )
            conn.commit()

    def _load_from_db(self):
        """Load non-expired provider records from SQLite on init."""
        try:
            now = time.monotonic()
            # Use wall-clock for persistence: store wall_time in DB, convert back
            # We store stored_at as wall time (time.time()) for cross-restart comparison
            wall_now = time.time()
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT skill_key_hash, node_id, host, port, sidecar_port,"
                    "       stored_at, ttl, skill_key"
                    " FROM kad_providers"
                ).fetchall()
            for row in rows:
                key_hash, node_id, host, port, sidecar_port, stored_at_wall, ttl, skill_key = row
                # Filter expired records on load
                age = wall_now - stored_at_wall
                if age > ttl:
                    continue
                # Convert wall-time age to monotonic for in-memory use
                monotonic_stored_at = now - age
                if key_hash not in self.cache:
                    self.cache[key_hash] = {}
                if node_id not in self.cache[key_hash]:
                    if self._total_records >= self.max_records:
                        break  # don't exceed capacity on load
                    self._total_records += 1
                rec = {
                    "node_id": node_id,
                    "host": host,
                    "port": int(port),
                    "sidecar_port": int(sidecar_port),
                    "stored_at": monotonic_stored_at,
                    "ttl": int(ttl),
                    "skill_key": skill_key,
                }
                self.cache[key_hash][node_id] = rec
                # KAD-08: push to heap
                heapq.heappush(self._evict_heap, (monotonic_stored_at, key_hash, node_id))
        except (sqlite3.Error, ValueError, TypeError):
            pass  # DB may be empty or corrupt — start fresh

    def _write_to_db(self, skill_key_hash: str, node_id: str, record: dict):
        """KAD-04: Write-through: persist a provider record to SQLite."""
        if not self._db_path:
            return
        # Store wall-clock time for cross-restart TTL comparison
        wall_time = time.time()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO kad_providers"
                    " (skill_key_hash, node_id, host, port, sidecar_port,"
                    "  stored_at, ttl, skill_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (skill_key_hash, node_id, record["host"], record["port"],
                     record["sidecar_port"], wall_time, record["ttl"], record["skill_key"]),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def _delete_from_db(self, skill_key_hash: str, node_id: str):
        """KAD-04: Remove a provider record from SQLite."""
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "DELETE FROM kad_providers WHERE skill_key_hash = ? AND node_id = ?",
                    (skill_key_hash, node_id),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    # ---- end KAD-04 --------------------------------------------------------

    def store(self, skill_key: str, node_id: str, host: str, port: int, sidecar_port: int, ttl: int = 1800):
        """Store or update a provider record. KAD-04: write-through to SQLite if configured."""
        key = self._get_key(skill_key)
        now = time.monotonic()

        if key not in self.cache:
            self.cache[key] = {}

        if node_id not in self.cache[key]:
            # Enforce max records
            if self._total_records >= self.max_records:
                self._evict_oldest()
            self._total_records += 1

        record = {
            "node_id": node_id,
            "host": host,
            "port": port,
            "sidecar_port": sidecar_port,
            "stored_at": now,
            "ttl": ttl,
            "skill_key": skill_key  # Store original key for searching
        }
        self.cache[key][node_id] = record
        # KAD-08: push to min-heap for O(log N) eviction
        heapq.heappush(self._evict_heap, (now, key, node_id))
        # KAD-04: write-through persistence
        self._write_to_db(key, node_id, record)

    def _evict_oldest(self):
        """KAD-08: Evict the oldest record using min-heap for O(log N) performance.

        Lazy cleanup: stale heap entries (records already removed from cache)
        are skipped without breaking eviction correctness.
        """
        while self._evict_heap:
            stored_at, key, node_id = heapq.heappop(self._evict_heap)
            # Lazy cleanup: skip if record no longer present or was updated (different stored_at)
            if key not in self.cache or node_id not in self.cache[key]:
                continue
            if self.cache[key][node_id]["stored_at"] != stored_at:
                # Record was re-stored (updated) with a newer timestamp
                continue
            # Found the actual oldest — evict it
            del self.cache[key][node_id]
            if not self.cache[key]:
                del self.cache[key]
            self._total_records -= 1
            self._delete_from_db(key, node_id)
            return
        # Heap empty but we're still over capacity — fallback linear scan
        # (should not happen in normal operation)
        for key, providers in list(self.cache.items()):
            for node_id in list(providers.keys()):
                del providers[node_id]
                self._total_records -= 1
                if not providers:
                    del self.cache[key]
                return

    def get_providers(self, skill_key: str) -> List[Dict]:
        """Return non-expired provider records for this skill key."""
        key = self._get_key(skill_key)
        providers = self.cache.get(key, {})
        now = time.monotonic()
        
        results = []
        for node_id, record in providers.items():
            if now - record["stored_at"] <= record["ttl"]:
                results.append(record)
        return results

    def search(self, query_value: str, query_type: str) -> List[Dict]:
        """Search cached providers by name or all."""
        now = time.monotonic()
        results = []
        
        if query_type == "all":
            for providers in self.cache.values():
                for record in providers.values():
                    if now - record["stored_at"] <= record["ttl"]:
                        results.append(record)
        elif query_type == "name":
            query_val_lower = query_value.lower()
            for providers in self.cache.values():
                for record in providers.values():
                    if record["skill_key"].lower() == query_val_lower:
                        if now - record["stored_at"] <= record["ttl"]:
                            results.append(record)
                            
        return results

    def remove(self, skill_key: str, node_id: str):
        """Remove a specific provider record. KAD-04: also deletes from SQLite."""
        key = self._get_key(skill_key)
        if key in self.cache and node_id in self.cache[key]:
            del self.cache[key][node_id]
            self._total_records -= 1
            if not self.cache[key]:
                del self.cache[key]
            # KAD-04: delete from DB
            self._delete_from_db(key, node_id)
            return True
        return False

    def evict_expired(self):
        """Remove all expired records."""
        now = time.monotonic()
        to_delete_keys = []
        
        for key, providers in self.cache.items():
            to_delete_nodes = [
                node_id for node_id, record in providers.items()
                if now - record["stored_at"] > record["ttl"]
            ]
            for node_id in to_delete_nodes:
                # TP-3: Delete from SQLite before removing from memory
                self._delete_from_db(key, node_id)
                del providers[node_id]
                self._total_records -= 1
            if not providers:
                to_delete_keys.append(key)

        for key in to_delete_keys:
            del self.cache[key]

        # TP-8: Periodic heap compaction — rebuild when stale entries dominate
        if self._evict_heap and len(self._evict_heap) > 3 * max(self._total_records, 1):
            new_heap = []
            for key_hash, providers in self.cache.items():
                for node_id, record in providers.items():
                    new_heap.append((record["stored_at"], key_hash, node_id))
            heapq.heapify(new_heap)
            self._evict_heap = new_heap

    def close(self):
        """KAD-04: No-op for write-through cache (data already persisted)."""
        pass

    def stats(self) -> Dict[str, int]:
        """Return cache statistics."""
        return {
            "total_records": self._total_records,
            "unique_skills": len(self.cache),
            "unique_providers": len(set(
                node_id for providers in self.cache.values() for node_id in providers
            ))
        }
