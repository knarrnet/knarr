import asyncio
import hashlib
import json
import logging
import uuid
from typing import List, Dict, Set, Optional, Callable

logger = logging.getLogger("knarr.plugin.kademlia.lookup")

class IterativeLookup:
    """Kademlia iterative lookup — finds k closest nodes to a target key."""

    def __init__(self, local_id: str, kbuckets, send_fn: Callable, k: int = 20, alpha: int = 3, timeout: float = 5.0):
        """
        Args:
            local_id: this node's ID
            kbuckets: the k-bucket table
            send_fn: async callable(peer_node_id, peer_host, peer_port, action, payload)
            k: number of closest nodes to find
            alpha: parallelism factor (concurrent queries per round)
            timeout: per-query timeout in seconds
        """
        self.local_id = local_id
        self.kbuckets = kbuckets
        self.send_fn = send_fn
        self.k = k
        self.alpha = alpha
        self.timeout = timeout
        self._pending: Dict[str, asyncio.Future] = {}  # request_id -> Future

    def resolve_response(self, request_id: str, payload: dict):
        """Called by handler.on_inbound when a response PluginMessage arrives."""
        if request_id in self._pending and not self._pending[request_id].done():
            self._pending[request_id].set_result(payload)

    def _is_valid_hex_id(self, node_id: str) -> bool:
        if not isinstance(node_id, str) or len(node_id) != 64:
            return False
        try:
            int(node_id, 16)
            return True
        except ValueError:
            return False

    def _get_distance(self, id1: str, id2: str) -> int:
        return int(id1, 16) ^ int(id2, 16)

    async def _send_and_wait(self, node_id: str, host: str, port: int, action: str, payload: dict) -> Optional[dict]:
        """Send a PluginMessage via fire-and-forget, wait for response via Future."""
        request_id = str(uuid.uuid4())
        payload["_request_id"] = request_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        try:
            await self.send_fn(node_id, host, port, action, payload)
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.debug(f"Lookup query failed to {node_id[:16]}: {e}")
            return None
        finally:
            self._pending.pop(request_id, None)

    async def find_nodes(self, target_id: str) -> List[Dict]:
        """Find k closest nodes to target_id."""
        if not self._is_valid_hex_id(target_id):
            return []

        # Seed from local k-buckets
        initial_candidates = self.kbuckets.get_closest(target_id, count=self.k)
        found: Dict[str, Dict] = {}
        for p in initial_candidates:
            found[p["node_id"]] = {
                "node_id": p["node_id"],
                "host": p["host"],
                "port": p["port"],
                "distance": self._get_distance(target_id, p["node_id"])
            }

        queried: Set[str] = {self.local_id}
        
        last_closest_ids = []

        for round_num in range(20):
            # Pick alpha closest unqueried nodes
            unqueried = [nid for nid in sorted(found.keys(), key=lambda nid: found[nid]["distance"])
                         if nid not in queried]
            
            targets = unqueried[:self.alpha]
            if not targets:
                break

            # Mark as queried immediately
            for tid in targets:
                queried.add(tid)

            # Parallel query
            tasks = []
            for tid in targets:
                node = found[tid]
                tasks.append(self._send_and_wait(node["node_id"], node["host"], node["port"], "FIND_NODE", {"target": target_id}))

            results = await asyncio.gather(*tasks)

            for res in results:
                if res and "peers" in res:
                    for p in res["peers"]:
                        nid = p.get("node_id")
                        if nid and nid != self.local_id and self._is_valid_hex_id(nid):
                            if nid not in found:
                                found[nid] = {
                                    "node_id": nid,
                                    "host": p["host"],
                                    "port": p["port"],
                                    "distance": self._get_distance(target_id, nid)
                                }

            # Convergence check
            closest_k = sorted(found.keys(), key=lambda nid: found[nid]["distance"])[:self.k]
            if closest_k == last_closest_ids:
                break
            last_closest_ids = closest_k

        return [found[nid] for nid in sorted(found.keys(), key=lambda nid: found[nid]["distance"])[:self.k]]

    async def find_providers(self, skill_key: str) -> List[Dict]:
        """Find providers for skill_key."""
        target_id = hashlib.sha256(skill_key.encode()).hexdigest()
        
        initial_candidates = self.kbuckets.get_closest(target_id, count=self.k)
        found: Dict[str, Dict] = {}
        for p in initial_candidates:
            found[p["node_id"]] = {
                "node_id": p["node_id"],
                "host": p["host"],
                "port": p["port"],
                "distance": self._get_distance(target_id, p["node_id"])
            }

        queried: Set[str] = {self.local_id}
        providers: Dict[str, Dict] = {} # node_id -> record
        
        last_closest_ids = []

        for round_num in range(20):
            unqueried = [nid for nid in sorted(found.keys(), key=lambda nid: found[nid]["distance"])
                         if nid not in queried]
            
            targets = unqueried[:self.alpha]
            if not targets:
                break

            for tid in targets:
                queried.add(tid)

            tasks = []
            for tid in targets:
                node = found[tid]
                tasks.append(self._send_and_wait(node["node_id"], node["host"], node["port"], "GET_PROVIDERS", {"skill_key": skill_key}))

            results = await asyncio.gather(*tasks)

            for res in results:
                if not res: continue
                
                # Collect providers
                if "providers" in res:
                    for p in res["providers"]:
                        providers[p["node_id"]] = p
                
                # Collect closer peers
                if "closer_peers" in res:
                    for p in res["closer_peers"]:
                        nid = p.get("node_id")
                        if nid and nid != self.local_id and self._is_valid_hex_id(nid):
                            if nid not in found:
                                found[nid] = {
                                    "node_id": nid,
                                    "host": p["host"],
                                    "port": p["port"],
                                    "distance": self._get_distance(target_id, nid)
                                }

            # Convergence check
            closest_k = sorted(found.keys(), key=lambda nid: found[nid]["distance"])[:self.k]
            if closest_k == last_closest_ids:
                break
            last_closest_ids = closest_k

        return list(providers.values())
