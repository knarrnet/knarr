import json
import asyncio
import inspect
import random
import logging
import time
import hashlib
from typing import List, Optional, Dict, Any

from knarr.core.messages import Message, Announce, Deregister, Query, PluginMessage
from knarr.core.models import NodeInfo
from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth

from kbuckets import KBucketTable
from providers import ProviderCache
from lookup import IterativeLookup

# KAD-01: Maximum single provider record payload size (4KB)
_MAX_PROVIDER_RECORD_SIZE = 4096
# KAD-01: Maximum providers per skill key
_MAX_PROVIDERS_PER_KEY = 100


def default_key_function(skill_name: str, canonical_path: str) -> bytes:
    """KAD-01: Pluggable key function. Returns SHA-256 of canonical_path.

    The canonical_path is the leaf-level classification path (e.g. "knowledge/translate").
    A future nomenclature standard can swap the hash input without touching STORE/FIND_VALUE logic.
    """
    return hashlib.sha256(canonical_path.encode()).digest()


class KademliaPlugin(PluginHooks):
    """Kademlia DHT passive cache plugin.

    Phase A: Observes gossip traffic to build k-bucket routing table
    and provider record cache. Logs cache hits on queries but does not
    send responses (no signing capability in PluginContext).

    Always returns True from on_inbound/on_outbound — purely passive.
    All hook bodies are wrapped in try/except to guarantee fail-open
    behavior: a malformed message must never cause message drops.
    """

    _SWEEP_SILENCE_THRESHOLD = 30.0
    _SWEEP_DEAD_THRESHOLD = 120.0

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._log = ctx.log
        self._debug = config.get("debug", False)
        self._sweep_bucket_idx: int = 0

        self.k = max(1, int(config.get("k", 8)))
        self.max_provider_records = max(1, int(config.get("max_provider_records", 50000)))
        self.evict_interval = float(config.get("evict_interval_seconds", 60))

        # Phase B config
        self.mode = config.get("mode", "passive")  # "passive", "full", or "backbone"
        if self.mode == "backbone":
            self.mode = "full"
            self.max_provider_records = max(self.max_provider_records, 500000)
        
        self.alpha = max(1, int(config.get("alpha", 3)))
        self.lookup_timeout = float(config.get("lookup_timeout_seconds", 5.0))
        self.max_find_node_per_minute = int(config.get("max_find_node_per_minute", 10))
        self._find_node_log: Dict[str, List[float]] = {}  # node_id -> [timestamps]

        self.kbuckets = KBucketTable(ctx.node_id, k=self.k)
        self.providers = ProviderCache(max_records=self.max_provider_records)

        if self.mode == "full":
            self._lookup = IterativeLookup(
                local_id=ctx.node_id,
                kbuckets=self.kbuckets,
                send_fn=self._send_plugin_message,
                k=self.k,
                alpha=self.alpha,
                timeout=self.lookup_timeout
            )
        else:
            self._lookup = None

        # Seed k-buckets from existing gossip peer table
        for peer in ctx.get_peers():
            self.kbuckets.add_peer(peer.node_id, peer.host, peer.port)

        self._last_evict = time.monotonic()
        self._kad_put_failed: Dict[str, bool] = {}  # B4: per-skill PUT failure flag for gossip fallback
        if self._debug:
            self._log.info(f"KAD_INIT mode={self.mode} k={self.k} max_records={self.max_provider_records}")

    def _resolve_peer(self, node_id: str, peers: list) -> Optional[NodeInfo]:
        """Find NodeInfo for a node_id from the gossip peer table."""
        for p in peers:
            if p.node_id == node_id:
                return p
        return None

    @staticmethod
    def _is_valid_hex_id(value: str) -> bool:
        """Check if a string is a valid hex node_id (non-empty, even length, hex chars)."""
        if not isinstance(value, str) or not value:
            return False
        try:
            bytes.fromhex(value)
            return len(value) % 2 == 0 and len(value) == 64
        except (ValueError, TypeError):
            return False

    async def _send_plugin_message(self, node_id: str, host: str, port: int, action: str, payload: dict):
        """Send a PluginMessage via fire-and-forget."""
        target_info = NodeInfo(node_id=node_id, host=host, port=port)
        msg = PluginMessage(
            node_id=self._ctx.node_id,
            plugin_name="knarr-kademlia",
            action=action,
            payload=json.dumps(payload)
        )
        await self._ctx.send_fire_forget(target_info, msg)

    async def _handle_plugin_message(self, msg: PluginMessage, peer_ip: str, peers: list):
        """Handle incoming PluginMessage actions."""
        action = getattr(msg, "action", "")
        sender_id = getattr(msg, "node_id", "")

        try:
            payload = json.loads(msg.payload) if msg.payload else {}
        except (json.JSONDecodeError, TypeError):
            return

        if action in ("FIND_NODE_RESP", "GET_PROVIDERS_RESP", "FIND_VALUE_RESP"):
            request_id = payload.get("_request_id", "")
            if self._lookup and request_id:
                self._lookup.resolve_response(request_id, payload)
            return

        # Rate limit by peer_ip to prevent Sybil bypass via identity rotation
        if action in ("FIND_NODE", "PUT_PROVIDER", "STORE", "GET_PROVIDERS", "FIND_VALUE"):
            rate_key = peer_ip or sender_id
            if not self._check_rate_limit(rate_key):
                return

        if action == "FIND_NODE":
            await self._handle_find_node(msg, payload, peers)
        elif action in ("PUT_PROVIDER", "STORE"):
            await self._handle_put_provider(msg, payload, peers)
        elif action in ("GET_PROVIDERS", "FIND_VALUE"):
            await self._handle_get_providers(msg, payload, peers)
        else:
            if self._debug:
                self._log.info(f"KAD_UNKNOWN_ACTION action={action} from={sender_id[:16]}")

    async def _handle_find_node(self, msg: PluginMessage, payload: dict, peers: list):
        """Respond to FIND_NODE with k closest peers."""
        sender_id = msg.node_id

        target = payload.get("target", "")
        if not self._is_valid_hex_id(target):
            return

        closest = self.kbuckets.get_closest(target, count=self.k)
        peer_list = [{"node_id": p["node_id"], "host": p["host"], "port": p["port"]} for p in closest]

        request_id = payload.get("_request_id", "")
        resp_data = {"peers": peer_list}
        if request_id:
            resp_data["_request_id"] = request_id

        resp = PluginMessage(
            node_id=self._ctx.node_id,
            plugin_name="knarr-kademlia",
            action="FIND_NODE_RESP",
            payload=json.dumps(resp_data)
        )
        sender_info = self._resolve_peer(sender_id, peers)
        if sender_info:
            await self._ctx.send_fire_forget(sender_info, resp)
            if self._debug:
                self._log.info(f"KAD_FIND_NODE target={target[:16]} closest={len(peer_list)} requester={sender_id[:16]}")

    async def _handle_put_provider(self, msg: PluginMessage, payload: dict, peers: list):
        """Store a provider record from a remote PUT_PROVIDER.
        V19-003: Bind identity to authenticated sender, resolve endpoint from peer table.
        KAD-01: 4KB record limit, max providers per key, dedup by node_id."""
        if self.mode != "full":
            return

        sender_id = msg.node_id

        skill_key = payload.get("skill_key", "")
        if not skill_key or not self._is_valid_hex_id(sender_id):
            return

        # KAD-01: Validate record size (4KB max)
        record_size = len(json.dumps(payload).encode("utf-8"))
        if record_size > _MAX_PROVIDER_RECORD_SIZE:
            if self._debug:
                self._log.info(f"KAD_PUT_REJECTED_SIZE skill={skill_key} size={record_size} max={_MAX_PROVIDER_RECORD_SIZE}")
            return

        # V19-003: Use authenticated sender identity (msg.node_id from signed envelope),
        # NOT self-asserted payload.node_id. Resolve endpoint from peer table.
        peer_info = self._resolve_peer(sender_id, peers)
        host = peer_info.host if peer_info else ""
        port = peer_info.port if peer_info else 0
        raw_sidecar_port = int(payload.get("sidecar_port", 0) or 0)
        sidecar_port = raw_sidecar_port if 1024 <= raw_sidecar_port <= 65535 else 0

        # KAD-01: Build provider record with skill-record semantics
        canonical_path = payload.get("canonical_path", skill_key)
        ttl = min(int(payload.get("ttl", 3600)), 7200)  # cap at 2h

        # KAD-01: Enforce max providers per key — dedup by node_id (latest wins),
        # evict oldest on overflow
        hashed_key = self.providers._get_key(skill_key)
        existing = self.providers.cache.get(hashed_key, {})
        if sender_id not in existing and len(existing) >= _MAX_PROVIDERS_PER_KEY:
            # Evict oldest provider for this key
            oldest_nid = min(existing, key=lambda nid: existing[nid].get("stored_at", 0))
            self.providers.remove(skill_key, oldest_nid)
            if self._debug:
                self._log.info(f"KAD_EVICT_OLDEST skill={skill_key} evicted={oldest_nid[:16]}")

        self.providers.store(skill_key, sender_id, host, port, sidecar_port, ttl=ttl)
        if self._debug:
            self._log.info(f"KAD_PUT_PROVIDER skill={skill_key} provider={sender_id[:16]} path={canonical_path}")

    async def _handle_get_providers(self, msg: PluginMessage, payload: dict, peers: list):
        """Respond to GET_PROVIDERS/FIND_VALUE with cached providers + closest nodes.
        KAD-01: Canonical response shape — providers populated if found, closest_nodes as fallback."""
        sender_id = msg.node_id

        skill_key = payload.get("skill_key", "")
        if not skill_key:
            return

        providers = self.providers.get_providers(skill_key)
        target = hashlib.sha256(skill_key.encode()).hexdigest()
        closest = self.kbuckets.get_closest(target, count=self.k)
        closer_peers = [{"node_id": p["node_id"], "host": p["host"], "port": p["port"]} for p in closest]

        request_id = payload.get("_request_id", "")
        # KAD-01: Canonical response shape
        resp_data = {
            "providers": providers,
            "closest_nodes": closer_peers if not providers else [],
            "closer_peers": closer_peers,  # backward compat
        }
        if request_id:
            resp_data["_request_id"] = request_id

        resp = PluginMessage(
            node_id=self._ctx.node_id,
            plugin_name="knarr-kademlia",
            action="GET_PROVIDERS_RESP",
            payload=json.dumps(resp_data)
        )
        sender_info = self._resolve_peer(sender_id, peers)
        if sender_info:
            await self._ctx.send_fire_forget(sender_info, resp)
            if self._debug:
                self._log.info(f"KAD_GET_PROVIDERS skill={skill_key} results={len(providers)} requester={sender_id[:16]}")

    _RATE_LIMIT_MAX_PEERS = 1000  # V19-005: Cap to prevent memory exhaustion under identity churn

    def _check_rate_limit(self, rate_key: str) -> bool:
        """Rate limit FIND_NODE/GET_PROVIDERS/PUT_PROVIDER: max N per minute per peer IP.

        Keyed by peer_ip to prevent Sybil bypass via identity rotation.
        """
        now = time.monotonic()
        window = 60.0

        # V19-005: Evict oldest entry if at capacity
        if rate_key not in self._find_node_log and len(self._find_node_log) >= self._RATE_LIMIT_MAX_PEERS:
            oldest_key = min(self._find_node_log, key=lambda k: max(self._find_node_log[k]) if self._find_node_log[k] else 0)
            del self._find_node_log[oldest_key]

        if rate_key not in self._find_node_log:
            self._find_node_log[rate_key] = []

        self._find_node_log[rate_key] = [t for t in self._find_node_log[rate_key] if now - t < window]

        if len(self._find_node_log[rate_key]) >= self.max_find_node_per_minute:
            return False

        self._find_node_log[rate_key].append(now)
        return True

    async def on_inbound(self, msg: Message, peer_ip: str) -> bool:
        """Observe traffic and handle Kademlia RPCs. Returns False if message handled."""
        try:
            peers = self._ctx.get_peers()

            # 1. Learn peer from any message with a node_id
            sender_id = getattr(msg, "node_id", "")
            if sender_id and sender_id != self._ctx.node_id and self._is_valid_hex_id(sender_id):
                peer_info = self._resolve_peer(sender_id, peers)
                if peer_info:
                    self.kbuckets.add_peer(sender_id, peer_info.host, peer_info.port)

            # 2. Learn provider from Announce (prefer peer table, fall back to peer_ip)
            if isinstance(msg, Announce) and self._is_valid_hex_id(getattr(msg, "node_id", "")):
                skill_key = getattr(msg, "skill_key", "")
                if isinstance(skill_key, str) and skill_key:
                    prov_info = self._resolve_peer(msg.node_id, peers)
                    host = prov_info.host if prov_info else peer_ip
                    port = prov_info.port if prov_info else 0
                    self.providers.store(
                        skill_key, msg.node_id, host,
                        port, getattr(msg, "sidecar_port", 0)
                    )
                    if self._debug:
                        src = "peer_table" if prov_info else "peer_ip"
                        self._log.info(f"KAD_LEARN skill={skill_key} provider={msg.node_id[:16]} src={src}")

            # 3. Remove provider on Deregister
            elif isinstance(msg, Deregister):
                skill_key = getattr(msg, "skill_key", "")
                node_id = getattr(msg, "node_id", "")
                if isinstance(skill_key, str) and skill_key and isinstance(node_id, str) and node_id:
                    if self.providers.remove(skill_key, node_id):
                        if self._debug:
                            self._log.info(f"KAD_FORGET skill={skill_key} provider={node_id[:16]}")

            # 4. Log cache status on Query (no response — PluginContext lacks signing key,
            #    unsigned QueryResponse would be rejected by signature-verifying nodes)
            elif isinstance(msg, Query):
                query_value = getattr(msg, "value", "")
                query_type = getattr(msg, "query_type", "")
                if isinstance(query_value, str) and isinstance(query_type, str):
                    results = self.providers.search(query_value, query_type)
                    if results:
                        if self._debug:
                            self._log.info(f"KAD_CACHE_HIT query={query_value} results={len(results)}")
                    else:
                        if self._debug:
                            self._log.info(f"KAD_CACHE_MISS query={query_value}")

            # 5. Handle PluginMessage addressed to us
            if isinstance(msg, PluginMessage) and getattr(msg, "plugin_name", "") == "knarr-kademlia":
                await self._handle_plugin_message(msg, peer_ip, peers)
                return True  # handled internally, but let chain continue (firewall sees it)

        except Exception as e:
            self._log.warning(f"KAD_INBOUND_ERR {type(e).__name__}: {e}")

        return True

    async def on_query(self, query_type: str, value: str) -> List[Dict[str, Any]]:
        """Search KAD provider cache, then network if cache misses (full mode)."""
        try:
            results = self.providers.search(value, query_type)

            # Active lookup on cache miss (full mode, name queries only)
            if not results and self._lookup and query_type == "name":
                try:
                    remote = await self._lookup.find_providers(value)
                    for p in remote:
                        nid = p.get("node_id", "")
                        if nid and self._is_valid_hex_id(nid):
                            self.providers.store(
                                p.get("skill_key", value), nid,
                                p.get("host", ""), int(p.get("port", 0)),
                                int(p.get("sidecar_port", 0))
                            )
                            results.append(p)
                    if self._debug:
                        self._log.info(f"KAD_LOOKUP type={query_type} value={value} found={len(remote)}")
                except Exception as e:
                    self._log.warning(f"KAD_LOOKUP_ERR {type(e).__name__}: {e}")

            query_results = []
            for record in results:
                query_results.append({
                    "node_id": record["node_id"],
                    "host": record["host"],
                    "port": record["port"],
                    "sidecar_port": record.get("sidecar_port", 0),
                    "skill_key": record.get("skill_key", value),
                    "_source": "kad",
                })

            if self._debug and query_results:
                self._log.info(f"KAD_QUERY type={query_type} value={value} results={len(query_results)}")
            return query_results
        except Exception as e:
            self._log.warning(f"KAD_QUERY_ERR {type(e).__name__}: {e}")
            return []

    async def on_outbound(self, msg: Message, peer: NodeInfo) -> bool:
        """Learn peers and own announcements from outbound traffic. Always returns True."""
        try:
            # Learn outbound target (we have full NodeInfo here)
            if self._is_valid_hex_id(peer.node_id):
                self.kbuckets.add_peer(peer.node_id, peer.host, peer.port)

            # Learn own announcements only (not forwarded announces from other nodes)
            if isinstance(msg, Announce) and getattr(msg, "node_id", "") == self._ctx.node_id:
                skill_key = getattr(msg, "skill_key", "")
                if isinstance(skill_key, str) and skill_key:
                    # Use our own address from the peer table, not the recipient's
                    self.providers.store(
                        skill_key, msg.node_id, "",  # host unknown for self
                        0, getattr(msg, "sidecar_port", 0)
                    )
                    if self._debug:
                        self._log.info(f"KAD_LEARN_SELF skill={skill_key}")

                    # In full mode, also PUT to k-closest nodes (public skills only)
                    skill_sheet = getattr(msg, "skill_sheet", {}) or {}
                    visibility = skill_sheet.get("visibility", "public")
                    if self._lookup and self.mode == "full" and visibility == "public":
                        # KAD-01: Extract canonical_path from skill_sheet if available
                        canonical_path = skill_sheet.get("canonical_path", skill_key)
                        task = asyncio.create_task(self._put_provider_to_closest(skill_key, canonical_path))
                        # B4: D-007 Phase C — suppress gossip, KAD handles distribution
                        # If last PUT for this skill failed, allow gossip fallback this cycle
                        put_failures = getattr(self, '_kad_put_failed', {})
                        last_failed = put_failures.pop(skill_key, False)
                        def _on_put_done(t, sk=skill_key):
                            if not t.cancelled() and t.exception():
                                getattr(self, '_kad_put_failed', {})[sk] = True
                        task.add_done_callback(_on_put_done)
                        if last_failed:
                            if self._debug:
                                self._log.info(f"KAD_GOSSIP_FALLBACK skill={skill_key} (prev PUT failed)")
                            return True  # allow gossip this cycle as fallback
                        if self._debug:
                            self._log.info(f"KAD_GOSSIP_SUPPRESS skill={skill_key}")
                        return False

        except Exception as e:
            self._log.warning(f"KAD_OUTBOUND_ERR {type(e).__name__}: {e}")

        return True

    async def _put_provider_to_closest(self, skill_key: str, canonical_path: str = ""):
        """Actively place provider record at k-closest nodes to skill hash.
        KAD-01: Uses pluggable key function, includes canonical_path in payload."""
        try:
            # KAD-01: Use pluggable key function
            path = canonical_path or skill_key
            key_bytes = default_key_function(skill_key, path)
            target = key_bytes.hex()
            closest = await self._lookup.find_nodes(target)

            payload = {
                "skill_key": skill_key,
                "canonical_path": path,
                "node_id": self._ctx.node_id,
                "host": "",  # recipient fills from message metadata
                "port": 0,
                "sidecar_port": getattr(self._ctx, 'sidecar_port', 0),
                "ttl": 3600,
                "published_at": time.time(),
            }

            for peer in closest[:self.k]:
                await self._send_plugin_message(
                    peer["node_id"], peer["host"], peer["port"],
                    "PUT_PROVIDER", payload
                )

            if self._debug:
                self._log.info(f"KAD_PUT_BROADCAST skill={skill_key} path={path} targets={len(closest[:self.k])}")
        except Exception as e:
            self._log.warning(f"KAD_PUT_ERR skill={skill_key} err={e}")
            self._kad_put_failed[skill_key] = True

    async def _maybe_refresh_buckets(self):
        """Refresh stale k-buckets by looking up a random ID in that bucket's range."""
        now = time.monotonic()
        if not hasattr(self, '_bucket_last_refresh'):
            self._bucket_last_refresh: Dict[int, float] = {}

        refresh_interval = 3600  # 60 minutes

        for i, bucket in enumerate(self.kbuckets.buckets):
            last = self._bucket_last_refresh.get(i, 0)
            if now - last < refresh_interval:
                continue
            if not bucket:  # empty bucket — nothing to refresh
                continue

            # Generate random ID in bucket range [2^i, 2^(i+1))
            # XOR with local_id to get a target that falls in this bucket
            random_distance = random.randint(2**i, 2**(i+1) - 1)
            target_int = self.kbuckets.local_id_int ^ random_distance
            target_hex = format(target_int, '064x')

            try:
                await self._lookup.find_nodes(target_hex)
                self._bucket_last_refresh[i] = now
                if self._debug:
                    self._log.info(f"KAD_REFRESH bucket={i}")
            except Exception:
                pass  # Best effort

            break  # One bucket per tick (don't flood)

    def _is_well_covered(self, health: NodeHealth) -> bool:
        bucket_stats = self.kbuckets.get_bucket_stats()
        peers_in_table = sum(bucket_stats.values())
        filled_buckets = len(bucket_stats)
        return (
            (peers_in_table >= 10 and filled_buckets >= 5)
            or getattr(health, "peer_count", 0) >= 10
        )

    async def _sweep_k_buckets(self, health: NodeHealth) -> None:
        bucket_count = len(self.kbuckets.buckets)
        if bucket_count <= 0:
            return

        for _ in range(min(8, bucket_count)):
            bucket_idx = self._sweep_bucket_idx % 256
            self._sweep_bucket_idx = (self._sweep_bucket_idx + 1) % 256
            bucket = self.kbuckets.buckets[bucket_idx]
            if not bucket:
                continue

            node_id, host, port, last_seen = bucket[0]
            silence = time.monotonic() - last_seen

            try:
                if silence > self._SWEEP_DEAD_THRESHOLD:
                    self.kbuckets.remove_peer(node_id)
                    remove_result = self._ctx.remove_peer(node_id)
                    if inspect.isawaitable(remove_result):
                        await remove_result
                    continue

                if silence > self._SWEEP_SILENCE_THRESHOLD and not self._is_well_covered(health):
                    push_result = self._ctx.push_to_peer(node_id, host, port)
                    if inspect.isawaitable(push_result):
                        await push_result
            except Exception as e:
                self._log.warning(f"KAD_SWEEP_ERR {type(e).__name__}: {e}")

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        """Periodic maintenance: evict expired records, sync k-buckets."""
        try:
            now = time.monotonic()

            # Periodic eviction
            if now - self._last_evict > self.evict_interval:
                self.providers.evict_expired()
                self._last_evict = now

            # Refresh k-buckets from gossip peer table
            for peer in peers:
                self.kbuckets.add_peer(peer.node_id, peer.host, peer.port)

            # Bucket refresh (full mode only, every 60 min per bucket)
            if self._lookup and self.mode == "full":
                await self._maybe_refresh_buckets()

            # Gap Mitigations: Prune stale rate limit entries
            if hasattr(self, '_find_node_log'):
                stale = [k for k, v in self._find_node_log.items() if not v or now - max(v) > 120]
                for k in stale:
                    del self._find_node_log[k]

            await self._sweep_k_buckets(health)

            if self._debug:
                stats = self.providers.stats()
                b_stats = self.kbuckets.get_bucket_stats()
                self._log.info(
                    f"KAD_TICK buckets={len(b_stats)} providers={stats['total_records']}"
                    f" peers_in_table={sum(b_stats.values())}"
                )
        except Exception as e:
            self._log.warning(f"KAD_TICK_ERR {type(e).__name__}: {e}")

    async def on_shutdown(self) -> None:
        """Log final statistics."""
        try:
            stats = self.providers.stats()
            b_stats = self.kbuckets.get_bucket_stats()
            self._log.info(
                f"KAD_SHUTDOWN providers={stats['total_records']} peers={sum(b_stats.values())}"
            )
        except Exception as e:
            self._log.warning(f"KAD_SHUTDOWN_ERR {type(e).__name__}: {e}")
