import asyncio
import collections
import dataclasses
import hashlib
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set, Deque

from knarr.core.messages import (
    Message, Heartbeat, Announce, Deregister, TaskRequest,
    TaskStatus, TaskResult, Query, SyncRequest, SyncResponse,
    JoinRequest, Warn, Blocked, MailSync, MailPullReq, MailPullAck,
    verify_message, serialize_message
)
from knarr.core.models import NodeInfo
from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth

log = logging.getLogger(__name__)

# Types that require synchronous TCP response — pass through after rate check
_REQUEST_RESPONSE_TYPES = (JoinRequest, Query, SyncRequest, TaskRequest, Heartbeat, MailSync, MailPullReq)

@dataclasses.dataclass
class QueueItem:
    dedup_key: Tuple
    message: Message
    first_seen: float
    last_seen: float
    source_cert_id: Optional[str]
    source_ip: str
    priority: int = 0         # 0 = normal, 1 = QoS (task-related)
    duplicate_count: int = 0
    weight: int = 1

class HistoryBuffer:
    """Large dedup registry. Evicts by age and count cap."""
    def __init__(self, dedup_ttl_seconds: int = 300, max_size: int = 50000):
        self._entries: Dict[Tuple, QueueItem] = {}
        self._ttl = dedup_ttl_seconds
        self._max_size = max_size

    def contains(self, key: Tuple) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return False
        if time.monotonic() - entry.last_seen > self._ttl:
            del self._entries[key]
            return False
        return True

    def add(self, item: QueueItem):
        self._entries[item.dedup_key] = item
        # V016-008: Evict oldest entries when cap exceeded
        while len(self._entries) > self._max_size:
            oldest_key = next(iter(self._entries))
            del self._entries[oldest_key]

    def refresh(self, key: Tuple):
        entry = self._entries.get(key)
        if entry:
            entry.last_seen = time.monotonic()
            entry.duplicate_count += 1

    def cleanup(self):
        """Periodic sweep — call from on_tick."""
        cutoff = time.monotonic() - self._ttl
        stale = [k for k, v in self._entries.items() if v.last_seen < cutoff]
        for k in stale:
            del self._entries[k]

class SlidingWindow:
    """Tracks weighted counts per time window."""
    def __init__(self):
        # List of (timestamp, weight)
        self._events: Deque[Tuple[float, int]] = collections.deque()

    def add(self, weight: int):
        self._events.append((time.monotonic(), weight))

    def total(self, window_seconds: int) -> int:
        cutoff = time.monotonic() - window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        return sum(e[1] for e in self._events)

class OutboundManager:
    """Manages self-pacing, warn/block lists, penalty queue, control channel."""
    def __init__(self, ctx: PluginContext, config: Dict[str, Any]):
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        self._debug = config.get("debug", False)
        self._warn_list: Dict[str, Tuple[float, int]] = {}  # peer_id -> (expires_at, delay_ms)
        self._block_list: Dict[str, float] = {}             # peer_id -> expires_at
        self._penalty_queue: Deque[Tuple[Message, str]] = collections.deque()
        self._sent_buffer: Dict[str, float] = {}            # peer_id -> last_send_time
        
        self._announce_pace_ms = float(config.get("announce_pace_ms", 50))
        self._warn_honor_minutes = float(config.get("warn_honor_minutes", 30))
        self._block_honor_minutes = float(config.get("block_honor_minutes", 60))

    def set_warn(self, peer_id: str, delay_ms: int, ttl_minutes: Optional[float] = None):
        if delay_ms == 0:
            self._warn_list.pop(peer_id, None)
            return
        
        ttl = ttl_minutes if ttl_minutes is not None else self._warn_honor_minutes
        expires_at = time.monotonic() + (ttl * 60)
        self._warn_list[peer_id] = (expires_at, delay_ms)

    def set_block(self, peer_id: str, ttl_minutes: Optional[float] = None):
        ttl = ttl_minutes if ttl_minutes is not None else self._block_honor_minutes
        expires_at = time.monotonic() + (ttl * 60)
        self._block_list[peer_id] = expires_at

    async def on_outbound(self, msg: Message, peer: NodeInfo) -> bool:
        peer_id = peer.node_id
        now = time.monotonic()

        # 1. Blocked by peer (Heartbeats exempt — must flow to keep peer alive)
        if peer_id in self._block_list:
            if now < self._block_list[peer_id]:
                if not isinstance(msg, Heartbeat):
                    if self._debug:
                        self._log.info(f"OUT_BLOCK {type(msg).__name__} to={peer_id[:16]}")
                    return False
            else:
                del self._block_list[peer_id]

        # 2. Warned by peer
        warn_entry = self._warn_list.get(peer_id)
        if warn_entry:
            expires_at, delay_ms = warn_entry
            if now > expires_at:
                del self._warn_list[peer_id]
            elif delay_ms > 0:
                last_send = self._sent_buffer.get(peer_id)
                if last_send is not None:
                    elapsed = now - last_send
                    if elapsed < (delay_ms / 1000.0):
                        self._penalty_queue.append((msg, peer_id))
                        if self._debug:
                            self._log.info(f"OUT_DEFER {type(msg).__name__} to={peer_id[:16]} delay={delay_ms}ms")
                        return False
                else:
                    # First send to this peer - if they already warned us, maybe we should still respect it?
                    # Spec says "space our sends". If we haven't sent anything yet, 
                    # we don't need to space.
                    pass

        # 3. Send immediately (fire-and-forget: no pool lock, no response wait)
        await self._ctx.send_fire_forget(peer, msg)
        self._sent_buffer[peer_id] = now
        if self._debug:
            msg_type = type(msg).__name__
            self._log.info(f"OUT_SEND {msg_type} to={peer_id[:16]} ({peer.host}:{peer.port})")
        return False # SUPPRESS normal send because we sent it ourselves

    async def send_control(self, msg: Message, peer_id: str, peer_info: NodeInfo):
        """Control messages bypass everything (fire-and-forget)."""
        await self._ctx.send_fire_forget(peer_info, msg)

    async def drain_penalty_queue(self, peer_lookup: Dict[str, NodeInfo]):
        now = time.monotonic()
        remaining = collections.deque()

        while self._penalty_queue:
            msg, peer_id = self._penalty_queue.popleft()
            peer = peer_lookup.get(peer_id)
            if not peer:
                continue

            warn = self._warn_list.get(peer_id)
            if not warn or now > warn[0] or warn[1] == 0:
                await self._ctx.send_fire_forget(peer, msg)
                self._sent_buffer[peer_id] = now
                continue

            delay_ms = warn[1]
            last_send = self._sent_buffer.get(peer_id, 0)
            if now - last_send >= delay_ms / 1000.0:
                await self._ctx.send_fire_forget(peer, msg)
                self._sent_buffer[peer_id] = now
            else:
                remaining.append((msg, peer_id))

        self._penalty_queue = remaining

    def cleanup(self):
        now = time.monotonic()
        self._warn_list = {k: v for k, v in self._warn_list.items() if v[0] > now}
        self._block_list = {k: v for k, v in self._block_list.items() if v > now}

class FirewallPlugin(PluginHooks):
    def __init__(self, ctx: PluginContext, config: Dict[str, Any]):
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        self._running = True

        # Inbound State
        self._pending: Dict[Tuple, QueueItem] = collections.OrderedDict()
        self._history = HistoryBuffer(int(config.get("dedup_ttl_seconds", 300)))
        self._rate_counters: Dict[str, SlidingWindow] = collections.defaultdict(SlidingWindow)
        self._ip_rate_counters: Dict[str, SlidingWindow] = collections.defaultdict(SlidingWindow)  # SA-FW1: per-IP aggregate
        self._ip_blocklist: Dict[str, Tuple[float, Optional[str]]] = {} # ip -> (expires_at, cert_id)
        self._cert_blocklist: Dict[str, float] = {} # cert_id -> expires_at
        self._warn_timestamps: Dict[str, float] = {} # cert_id -> last_warn_time
        
        # Adaptive Limit State
        self._smoothed_fill = 0.0
        self._last_band_change = time.monotonic()
        self._current_pressure_multiplier = 1.0

        # Outbound State
        self._outbound = OutboundManager(ctx, config)

        # Config
        self._role = config.get("role", "leaf")
        self._pending_max = int(config.get("pending_queue_size", 2000))
        self._staleness_seconds = float(config.get("staleness_seconds", 20))
        self._base_limit = int(config.get("base_limit", 500))
        self._window_seconds = int(config.get("window_seconds", 60))
        self._ban_duration_minutes = int(config.get("ban_duration_minutes", 60))
        self._warn_cooldown = float(config.get("warn_cooldown_seconds", 10))
        self._processing_enabled = True
        self._debug = config.get("debug", False)

        # v0.26.0: Group-aware config
        groups_cfg = config.get("groups", {})
        self._block_groups = list(groups_cfg.get("block_groups", []))
        self._rate_multipliers = dict(groups_cfg.get("rate_multipliers", {}))
        self._qos_groups = list(groups_cfg.get("qos_priority", []))

        # A-04: Gateway IP exemption — bypass L1 rate limiting for Docker bridge gateway
        self._gateway_exempt: set = set(config.get("gateway_exempt", []))
        if self._debug and self._gateway_exempt:
            self._log.info(f"FIREWALL_GATEWAY_EXEMPT ips={sorted(self._gateway_exempt)}")

        # Background Loop
        self._process_task = asyncio.create_task(self._process_loop())

    async def on_connect(self, peer_ip: str) -> bool:
        """L0 — IP Blocklist (pre-auth)"""
        now = time.time()
        block = self._ip_blocklist.get(peer_ip)
        if block and block[0] > now:
            if self._debug:
                self._log.info(f"CONN_DROP ip={peer_ip} reason=ip_blocked")
            return False
        return True

    async def on_inbound(self, msg: Message, peer_ip: str) -> bool:
        now_wall = time.time()
        now_mono = time.monotonic()
        msg_type = type(msg).__name__
        sender_id = getattr(msg, 'node_id', '') or getattr(msg, 'requester_node_id', '')

        # L1 — Identity (Heartbeats exempt — must flow to keep peer alive)
        cert_id = self._get_cert_id(msg)
        if cert_id and cert_id in self._cert_blocklist and self._cert_blocklist[cert_id] > now_wall:
            if not isinstance(msg, Heartbeat):
                if self._debug:
                    self._log.info(f"IN_DROP {msg_type} from={sender_id[:16]} ip={peer_ip} reason=cert_blocked")
                return False

        # L1.5 — Group Blocklist (after identity, before rate check)
        if self._block_groups and cert_id:
            engine = self._ctx.group_engine
            if engine:
                for bg in self._block_groups:
                    if engine.is_member(cert_id, bg):
                        if self._debug:
                            self._log.info(f"IN_DROP {msg_type} from={sender_id[:16]} ip={peer_ip} reason=group_blocked group={bg}")
                        return False

        # A-04: Gateway IP exemption — Docker bridge gateway bypass for L1 rate limiting.
        # L3+ identity checks (Ed25519) still apply.
        _gateway_exempt = peer_ip in self._gateway_exempt
        if _gateway_exempt and self._debug:
            self._log.info(f"GATEWAY_EXEMPT ip={peer_ip} msg_type={msg_type}")

        # L4 — Rate Check (all messages, before type split)
        # Skip for gateway-exempt IPs (Docker bridge — all 150 nodes appear as one IP).
        counter_id = cert_id or peer_ip
        if not _gateway_exempt:
            counter = self._rate_counters[counter_id]
            weight = self._get_weight(msg)
            counter.add(weight)

            count = counter.total(self._window_seconds)
            limit = self._get_effective_limit(msg, cert_id)
            ratio = count / limit

            if ratio > 1.0:
                if self._debug:
                    self._log.info(f"IN_BAN {msg_type} from={sender_id[:16]} ip={peer_ip} ratio={ratio:.2f}")
                self._ban(cert_id, peer_ip, "Rate limit exceeded")
                # Heartbeats exempt — must flow to keep peer alive even during rate-limit
                if not isinstance(msg, Heartbeat):
                    return False

            # SA-FW1: Per-IP aggregate rate counter (defeats identity fragmentation)
            # Every message increments the IP counter regardless of identity.
            # IP limit is 2x base_limit — allows legitimate multi-identity use
            # but blocks single-IP floods via keypair rotation.
            ip_counter = self._ip_rate_counters[peer_ip]
            ip_counter.add(weight)
            ip_count = ip_counter.total(self._window_seconds)
            ip_limit = self._base_limit * 2
        else:
            weight = self._get_weight(msg)
            ratio = 0.0
            ip_count = 0
            ip_limit = 1
        if not _gateway_exempt and ip_count > ip_limit:
            if self._debug:
                self._log.info(f"IN_BAN_IP {msg_type} ip={peer_ip} ip_count={ip_count} ip_limit={ip_limit}")
            self._ip_blocklist[peer_ip] = (time.time() + self._ban_duration_minutes * 60, cert_id)
            self._log.warning(f"IP_BAN: ip={peer_ip} reason=aggregate rate {ip_count}/{ip_limit}")
            if not isinstance(msg, Heartbeat):
                return False

        if ratio > 0.85:
            await self._maybe_warn(cert_id, peer_ip, 500)
        elif ratio > 0.7:
            await self._maybe_warn(cert_id, peer_ip, 100)
        elif ratio > 0.5:
            await self._maybe_warn(cert_id, peer_ip, 20)

        # L5 — Warn/Block Reception
        # V016-001: Use cert_id (derived from public_key) so outbound key matches
        if isinstance(msg, Warn):
            sender = cert_id or peer_ip
            self._outbound.set_warn(sender, msg.delay_ms)
            if self._debug:
                self._log.info(f"THROTTLE_RECV_WARN from={sender_id[:16]} ip={peer_ip} delay_ms={msg.delay_ms}")
            return False
        if isinstance(msg, Blocked):
            sender = cert_id or peer_ip
            self._outbound.set_block(sender, msg.duration_minutes)
            if self._debug:
                self._log.info(f"THROTTLE_RECV_BLOCK from={sender_id[:16]} ip={peer_ip} duration={msg.duration_minutes}min")
            return False

        # Request/response types: pass through for synchronous TCP response.
        # These need the response sent on the same connection, so the node
        # must process them directly — not via the async queue.
        if isinstance(msg, _REQUEST_RESPONSE_TYPES):
            if self._debug:
                self._log.info(f"IN_PASS {msg_type} from={sender_id[:16]} ip={peer_ip}")
            return True

        # L3 — Deduplication (fire-and-forget types only)
        key = self._get_dedup_key(msg, cert_id, peer_ip)
        if key in self._pending:
            item = self._pending[key]
            item.last_seen = now_mono
            item.duplicate_count += 1
        elif self._history.contains(key):
            self._history.refresh(key)
            if self._debug:
                self._log.info(f"IN_DEDUP {msg_type} from={sender_id[:16]} ip={peer_ip}")
            return False
        else:
            # L6 — QoS priority assigned at insertion
            priority = 1 if isinstance(msg, (TaskResult, TaskStatus)) else 0
            # v0.26.0: Group-based QoS priority
            if self._qos_groups and cert_id:
                engine = self._ctx.group_engine
                if engine:
                    for gi, g in enumerate(self._qos_groups):
                        if engine.is_member(cert_id, g):
                            priority = max(priority, len(self._qos_groups) - gi + 1)
                            break
            item = QueueItem(
                dedup_key=key,
                message=msg,
                first_seen=now_mono,
                last_seen=now_mono,
                source_cert_id=cert_id,
                source_ip=peer_ip,
                weight=weight,
                priority=priority
            )
            self._pending[key] = item
            if len(self._pending) > self._pending_max:
                self._pending.popitem(last=False)

        if self._debug:
            self._log.info(f"IN_QUEUE {msg_type} from={sender_id[:16]} ip={peer_ip} pending={len(self._pending)}")
        return False

    def _get_cert_id(self, msg: Message) -> Optional[str]:
        # V016-003: Derive identity from public_key (signature-verified) not self-asserted fields
        if hasattr(msg, 'public_key') and msg.public_key:
            return hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
        return None

    def _get_dedup_key(self, msg: Message, cert_id: Optional[str], peer_ip: str) -> Tuple:
        if isinstance(msg, Announce):
            return ("ANNOUNCE", msg.node_id, msg.skill_key, msg.msg_id)
        if isinstance(msg, Deregister):
            return ("DEREGISTER", msg.node_id, msg.skill_key)
        if isinstance(msg, Heartbeat):
            return ("HEARTBEAT", msg.node_id)
        if isinstance(msg, TaskRequest):
            return ("TASK_REQUEST", msg.task_id)
        if isinstance(msg, TaskResult):
            return ("TASK_RESULT", msg.task_id)
        if isinstance(msg, TaskStatus):
            return ("TASK_STATUS", msg.task_id)
        if isinstance(msg, Query):
            return ("QUERY", msg.msg_id)
        if isinstance(msg, SyncRequest):
            return ("SYNC_REQUEST", cert_id or peer_ip)
        if isinstance(msg, JoinRequest):
            return ("JOIN_REQUEST", msg.node_id)
        return ("OTHER", msg.msg_id)

    def _get_weight(self, msg: Message) -> int:
        if isinstance(msg, SyncResponse):
            return max(1, len(msg.skills))
        if isinstance(msg, SyncRequest):
            return 10
        return 1

    def _get_effective_limit(self, msg: Message, cert_id: Optional[str]) -> int:
        pressure = self._current_pressure_multiplier
        role_mult = 1.0
        is_task = isinstance(msg, (TaskRequest, TaskStatus, TaskResult))
        is_gossip = isinstance(msg, (Announce, Deregister))

        if self._role == "bootstrap":
            if is_gossip: role_mult = 2.0
            if is_task: role_mult = 0.5
        elif self._role == "provider":
            if is_gossip: role_mult = 0.5
            if is_task: role_mult = 2.0

        # v0.26.0: Group rate multiplier — best multiplier wins (clamped to 10.0)
        group_mult = 1.0
        if self._rate_multipliers and cert_id:
            engine = self._ctx.group_engine
            if engine:
                for group_name, mult in self._rate_multipliers.items():
                    if engine.is_member(cert_id, group_name):
                        group_mult = max(group_mult, min(float(mult), 10.0))

        # V016-005: Clamp to min 1 to prevent division-by-zero in ratio calc
        return max(1, int(self._base_limit * pressure * role_mult * group_mult))

    async def _maybe_warn(self, cert_id: Optional[str], peer_ip: str, delay_ms: int):
        target = cert_id or peer_ip
        last = self._warn_timestamps.get(target, 0)
        if time.monotonic() - last < self._warn_cooldown:
            return

        self._warn_timestamps[target] = time.monotonic()
        fill_pct = int(100 * self._get_weighted_fill() / self._pending_max)
        warn_msg = Warn(delay_ms=delay_ms, reason="rate_limit", pressure_pct=fill_pct)
        peers = self._ctx.get_peers()
        peer_info = next((p for p in peers if p.node_id == cert_id), None)
        if peer_info:
            await self._outbound.send_control(warn_msg, cert_id, peer_info)
            if self._debug:
                self._log.info(f"THROTTLE_SEND_WARN to={cert_id[:16]} delay_ms={delay_ms} fill={fill_pct}%")

    def _ban(self, cert_id: Optional[str], ip_address: str, reason: str):
        now_wall = time.time()
        expires_wall = now_wall + (self._ban_duration_minutes * 60)

        if cert_id:
            # Cert-level throttle only — don't IP-ban authenticated peers.
            # IP blocklist severs the connection before message parsing,
            # which kills heartbeats and causes PRUNE_PEERS cascade.
            self._cert_blocklist[cert_id] = expires_wall
        else:
            # No cert = unauthenticated connection — IP-ban is appropriate
            self._ip_blocklist[ip_address] = (expires_wall, cert_id)

        self._log.warning(f"BANNED: cert_id={cert_id} ip={ip_address} reason={reason}")

        if cert_id:
            asyncio.create_task(self._send_blocked(cert_id, self._ban_duration_minutes))

    async def _send_blocked(self, cert_id: str, duration: int):
        peers = self._ctx.get_peers()
        peer_info = next((p for p in peers if p.node_id == cert_id), None)
        if peer_info:
            msg = Blocked(reason="rate_limit_exceeded", duration_minutes=duration)
            await self._outbound.send_control(msg, cert_id, peer_info)
            if self._debug:
                self._log.info(f"THROTTLE_SEND_BLOCK to={cert_id[:16]} duration={duration}min")

    def _get_weighted_fill(self) -> int:
        return sum(item.weight for item in self._pending.values())

    async def _process_loop(self):
        while self._running:
            if not self._pending or not self._processing_enabled:
                await asyncio.sleep(0.01)
                continue

            key_to_process = None
            best_prio = 0
            # Scan for highest-priority item (C-03 fix)
            for key, item in self._pending.items():
                if item.priority > best_prio:
                    best_prio = item.priority
                    key_to_process = key

            if not key_to_process:
                key_to_process = next(iter(self._pending))

            item = self._pending.pop(key_to_process)
            
            age = time.monotonic() - item.last_seen
            if age > self._staleness_seconds:
                self._history.add(item)
                continue

            asyncio.create_task(self._deliver(item))
            self._history.add(item)

    async def _deliver(self, item: QueueItem):
        if self._ctx.delivery_cb:
            await self._ctx.delivery_cb(item.message, item.source_ip)

    async def on_outbound(self, msg: Message, peer: NodeInfo) -> bool:
        return await self._outbound.on_outbound(msg, peer)

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        actual_fill = self._get_weighted_fill() / self._pending_max
        self._smoothed_fill = 0.8 * self._smoothed_fill + 0.2 * actual_fill
        
        now = time.monotonic()
        new_mult = 1.0
        if self._smoothed_fill >= 0.85: new_mult = 0.1
        elif self._smoothed_fill >= 0.6: new_mult = 0.25
        elif self._smoothed_fill >= 0.3: new_mult = 0.6
        else: new_mult = 1.0
        
        old_mult = self._current_pressure_multiplier
        if new_mult < self._current_pressure_multiplier:
            self._current_pressure_multiplier = new_mult
            self._last_band_change = now
        elif new_mult > self._current_pressure_multiplier:
            if now - self._last_band_change > 5.0:
                self._current_pressure_multiplier = min(new_mult, self._current_pressure_multiplier + 0.1)
                self._last_band_change = now
        if self._debug and self._current_pressure_multiplier != old_mult:
            self._log.info(f"PRESSURE_BAND {old_mult:.2f}→{self._current_pressure_multiplier:.2f} fill={self._smoothed_fill:.2f}")

        self._history.cleanup()
        self._outbound.cleanup()
        
        peer_lookup = {p.node_id: p for p in peers}
        await self._outbound.drain_penalty_queue(peer_lookup)

        now_wall = time.time()
        expired_ips = [ip for ip, (exp, _) in self._ip_blocklist.items() if exp <= now_wall]
        for ip in expired_ips: del self._ip_blocklist[ip]
        
        expired_certs = [cid for cid, exp in self._cert_blocklist.items() if exp <= now_wall]
        for cid in expired_certs: del self._cert_blocklist[cid]

    async def on_shutdown(self) -> None:
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
