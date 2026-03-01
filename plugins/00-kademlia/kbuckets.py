import time
from typing import List, Tuple, Dict, Optional

class KBucketTable:
    """K-bucket routing table for Kademlia. XOR distance based."""
    
    def __init__(self, local_id: str, k: int = 20):
        self.local_id = local_id
        self.local_id_int = int.from_bytes(bytes.fromhex(local_id), 'big')
        self.k = k
        # 256 buckets, each is a list of [node_id, host, port, last_seen]
        # Most recently seen is at the end.
        self.buckets: List[List[List]] = [[] for _ in range(256)]

    def _get_distance(self, node_id: str) -> int:
        peer_id_int = int.from_bytes(bytes.fromhex(node_id), 'big')
        return self.local_id_int ^ peer_id_int

    def _get_bucket_index(self, distance: int) -> int:
        if distance == 0:
            return -1
        return distance.bit_length() - 1

    def add_peer(self, node_id: str, host: str, port: int):
        """Add or update a peer in the routing table."""
        if node_id == self.local_id:
            return

        distance = self._get_distance(node_id)
        idx = self._get_bucket_index(distance)
        if idx < 0:
            return

        bucket = self.buckets[idx]
        now = time.monotonic()

        # Check if already in bucket
        for i, peer in enumerate(bucket):
            if peer[0] == node_id:
                # Move to tail
                bucket.pop(i)
                bucket.append([node_id, host, port, now])
                return

        # If bucket not full, append
        if len(bucket) < self.k:
            bucket.append([node_id, host, port, now])
        # If full, do NOT evict in Phase A (passive)

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
                return

    def get_closest(self, target_id: str, count: int = 20) -> List[Dict]:
        """Return closest peers to target_id, sorted by XOR distance."""
        target_int = int.from_bytes(bytes.fromhex(target_id), 'big')
        
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
