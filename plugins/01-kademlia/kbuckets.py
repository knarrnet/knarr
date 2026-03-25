import sqlite3
import time
from typing import List, Tuple, Dict, Optional

class KBucketTable:
    """K-bucket routing table for Kademlia. XOR distance based.

    KAD-04: Supports optional SQLite persistence. Pass db_path to enable.
    Schema: kad_routing (node_id TEXT PK, host TEXT, port INT, bucket INT, last_seen REAL)
    """

    # KAD-04: Checkpoint interval (seconds)
    _CHECKPOINT_INTERVAL = 300.0

    def __init__(self, local_id: str, k: int = 20, db_path: Optional[str] = None):
        self.local_id = local_id
        self.local_id_int = int.from_bytes(bytes.fromhex(local_id), 'big')
        self.k = k
        # 256 buckets, each is a list of [node_id, host, port, last_seen]
        # Most recently seen is at the end.
        self.buckets: List[List[List]] = [[] for _ in range(256)]
        self._db_path = db_path
        self._last_checkpoint = time.monotonic()
        # KAD-09: pending evictions: oldest_node_id -> (candidate_id, host, port, added_at)
        self._pending_evictions: dict = {}

        if self._db_path:
            self._init_db()
            self._load_from_db()

    # ---- KAD-04: SQLite persistence ----------------------------------------

    def _init_db(self):
        """Create the routing table schema if it doesn't exist."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kad_routing ("
                "  node_id TEXT PRIMARY KEY,"
                "  host TEXT,"
                "  port INTEGER,"
                "  bucket INTEGER,"
                "  last_seen REAL"
                ")"
            )
            conn.commit()

    def _load_from_db(self):
        """Load routing table from SQLite on init.

        KAD-04: DB stores wall-clock timestamps. On load we convert back to
        monotonic by computing age = now_real - last_seen_real and reconstructing
        last_seen_mono = now_mono - age. This survives process restarts where
        time.monotonic() resets to near zero.
        """
        try:
            now_real = time.time()
            now_mono = time.monotonic()
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT node_id, host, port, bucket, last_seen FROM kad_routing"
                ).fetchall()
            for row in rows:
                node_id, host, port, bucket_idx, last_seen_real = row
                if 0 <= bucket_idx < 256 and node_id != self.local_id:
                    # TP-12: Use add_peer() so IP diversity and other checks are applied
                    self.add_peer(node_id, host, int(port))
        except (sqlite3.Error, ValueError):
            pass  # DB may be empty or corrupt — start fresh

    def _save_peer_to_db(self, node_id: str, host: str, port: int, bucket_idx: int, last_seen: float):
        """Upsert a single peer into the DB (write-through on updates if desired).

        KAD-04: last_seen is monotonic — convert to wall-clock for persistence.
        """
        if not self._db_path:
            return
        try:
            # Convert monotonic to wall-clock
            now_real = time.time()
            now_mono = time.monotonic()
            age = max(0.0, now_mono - last_seen)
            last_seen_real = max(0.0, now_real - age)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO kad_routing (node_id, host, port, bucket, last_seen)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (node_id, host, port, bucket_idx, last_seen_real),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def _delete_peer_from_db(self, node_id: str):
        """Remove a peer from the DB."""
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM kad_routing WHERE node_id = ?", (node_id,))
                conn.commit()
        except sqlite3.Error:
            pass

    # ---- KAD-09: Ping-before-evict support --------------------------------

    def get_pending_evictions(self) -> dict:
        """Return the pending evictions dict: {oldest_node_id: (candidate_id, host, port, ts)}."""
        return dict(self._pending_evictions)

    def resolve_evict_oldest(self, oldest_node_id: str):
        """KAD-09: Oldest did not PONG in time — evict it, add the new candidate."""
        if oldest_node_id not in self._pending_evictions:
            return
        candidate_id, candidate_host, candidate_port, added_at = self._pending_evictions.pop(oldest_node_id)
        self.remove_peer(oldest_node_id)
        # Now add candidate — bucket has space
        self.add_peer(candidate_id, candidate_host, candidate_port)
        if self._debug if hasattr(self, '_debug') else False:
            pass  # logging handled by handler

    def resolve_keep_oldest(self, oldest_node_id: str):
        """KAD-09: Oldest PONGed — move to tail (fresh), drop the new candidate."""
        if oldest_node_id not in self._pending_evictions:
            return
        self._pending_evictions.pop(oldest_node_id)
        # Move oldest to tail (it's alive)
        distance = self._get_distance(oldest_node_id)
        idx = self._get_bucket_index(distance)
        if idx < 0:
            return
        bucket = self.buckets[idx]
        now = time.monotonic()
        for i, peer in enumerate(bucket):
            if peer[0] == oldest_node_id:
                bucket.pop(i)
                bucket.append([oldest_node_id, peer[1], peer[2], now])
                return

    # ---- end KAD-09 --------------------------------------------------------

    def maybe_checkpoint(self):
        """KAD-04: Periodic checkpoint (called from on_tick every 5 min).
        Writes all current peers to DB.
        """
        if not self._db_path:
            return
        now = time.monotonic()
        if now - self._last_checkpoint < self._CHECKPOINT_INTERVAL:
            return
        self._last_checkpoint = now
        self._flush_to_db()

    def _flush_to_db(self):
        """Write all in-memory peers to DB.

        KAD-04: Converts monotonic last_seen to wall-clock for cross-restart
        persistence. age = now_mono - last_seen_mono; last_seen_real = now_real - age.
        """
        if not self._db_path:
            return
        try:
            now_real = time.time()
            now_mono = time.monotonic()
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM kad_routing")
                for bucket_idx, bucket in enumerate(self.buckets):
                    for peer in bucket:
                        # Convert monotonic timestamp to wall-clock
                        age = max(0.0, now_mono - float(peer[3]))
                        last_seen_real = max(0.0, now_real - age)
                        conn.execute(
                            "INSERT OR REPLACE INTO kad_routing"
                            " (node_id, host, port, bucket, last_seen)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (peer[0], peer[1], peer[2], bucket_idx, last_seen_real),
                        )
                conn.commit()
        except sqlite3.Error:
            pass

    def save_on_shutdown(self):
        """KAD-04: Flush all peers to DB on shutdown."""
        self._flush_to_db()

    def checkpoint(self):
        """KAD-04: Force-flush peers to DB (no interval guard)."""
        self._flush_to_db()

    def close(self):
        """KAD-04: Flush and close (alias for save_on_shutdown)."""
        self._flush_to_db()

    # ---- end KAD-04 --------------------------------------------------------

    def _get_distance(self, node_id: str) -> int:
        peer_id_int = int.from_bytes(bytes.fromhex(node_id), 'big')
        return self.local_id_int ^ peer_id_int

    def _get_bucket_index(self, distance: int) -> int:
        if distance == 0:
            return -1
        return distance.bit_length() - 1

    # KAD-12: Max nodes from same IP per bucket (allows legitimate NAT, blocks bulk Sybil)
    _MAX_SAME_IP_PER_BUCKET = 2

    @staticmethod
    def _normalize_host(host: str) -> str:
        """KAD-12: Normalize host string for consistent IP counting."""
        return str(host or "").strip()

    def _count_ip_in_bucket(self, bucket: list, host: str) -> int:
        """KAD-12: Count how many entries in bucket share the same IP."""
        normalized = self._normalize_host(host)
        return sum(1 for peer in bucket if self._normalize_host(peer[1]) == normalized)

    def add_peer(self, node_id: str, host: str, port: int):
        """Add or update a peer in the routing table.

        KAD-12: Cap entries per IP per bucket at _MAX_SAME_IP_PER_BUCKET to
        prevent Sybil eclipse from a single machine.
        """
        if node_id == self.local_id:
            return

        distance = self._get_distance(node_id)
        idx = self._get_bucket_index(distance)
        if idx < 0:
            return

        bucket = self.buckets[idx]
        now = time.monotonic()

        # Check if already in bucket — update in place (move to tail)
        for i, peer in enumerate(bucket):
            if peer[0] == node_id:
                bucket.pop(i)
                bucket.append([node_id, host, port, now])
                return

        # KAD-12: Reject if this IP already has _MAX_SAME_IP_PER_BUCKET entries
        if self._count_ip_in_bucket(bucket, host) >= self._MAX_SAME_IP_PER_BUCKET:
            return

        # If bucket not full, append
        if len(bucket) < self.k:
            bucket.append([node_id, host, port, now])
        else:
            # KAD-09: Bucket full — mark oldest (head) for pending eviction.
            # The caller (handler.on_tick) issues a PING and resolves eviction.
            oldest = bucket[0]
            self._pending_evictions[oldest[0]] = (node_id, host, port, now)

    def remove_peer(self, node_id: str):
        """Remove a peer from the routing table."""
        distance = self._get_distance(node_id)
        idx = self._get_bucket_index(distance)
        if idx < 0:
            return

        bucket = self.buckets[idx]
        for i, peer in enumerate(bucket):
            if peer[0] == node_id:
                bucket.pop(i)
                # TP-9: Delete from SQLite immediately (don't wait for checkpoint)
                self._delete_peer_from_db(node_id)
                # TP-10: Clean up any pending eviction referencing this node
                if node_id in self._pending_evictions:
                    self._pending_evictions.pop(node_id)
                stale_evictions = [
                    oldest for oldest, (cand, _, _, _) in self._pending_evictions.items()
                    if cand == node_id
                ]
                for oldest in stale_evictions:
                    self._pending_evictions.pop(oldest)
                return

    def get_closest(self, target_id: str, count: int = 20) -> List[Dict]:
        """Return closest peers to target_id, sorted by XOR distance.

        KAD-15: Validate target_id before processing.
        Short hex input produces wrong XOR distances; non-hex input raises ValueError.
        Both are reachable from attacker-controlled PluginMessage payloads.
        Returns empty list on invalid input instead of raising.
        """
        # KAD-15: Must be exactly 64 hex characters (32 bytes / 256-bit node ID)
        if not isinstance(target_id, str) or len(target_id) != 64:
            return []
        try:
            target_int = int.from_bytes(bytes.fromhex(target_id), 'big')
        except (ValueError, TypeError):
            return []

        all_peers = []
        for bucket in self.buckets:
            for peer in bucket:
                dist = target_int ^ int.from_bytes(bytes.fromhex(peer[0]), 'big')
                all_peers.append({
                    "node_id": peer[0],
                    "host": peer[1],
                    "port": peer[2],
                    "distance": dist
                })

        all_peers.sort(key=lambda x: x["distance"])
        return all_peers[:count]

    def get_bucket_stats(self) -> Dict[int, int]:
        """Return non-empty bucket statistics."""
        return {i: len(b) for i, b in enumerate(self.buckets) if b}
