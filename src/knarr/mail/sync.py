import asyncio
import logging
import time
import json
import uuid
from typing import List, Dict, Any, Optional, Callable
from ..core.messages import MailSync, MailAck, MailPullReq, MailPullResp, MailPullAck

logger = logging.getLogger("knarr.mail.sync")

class SyncEngine:
    """Core protocol store-and-forward mail sync engine."""

    def __init__(self, node, plugins=None):
        self._node = node
        self._plugins = plugins  # M-016: PluginLoader, wired after init
        self._mail_handlers: Dict[str, Callable] = {}
        self._push_in_flight: set = set()  # V17-005: per-peer singleflight
        self._pull_last_req: Dict[str, float] = {}  # node_id -> monotonic time of last pull req
        self._notified_stale: set = set()  # F20: dedup stale inbox alerts (capped)
        self._log = logger
        self._debug = node._config.get("mail", {}).get("debug", False)

    def register_handler(self, msg_type: str, handler: Callable):
        """Register a dispatch handler for a system mail msg_type."""
        self._mail_handlers[msg_type] = handler

    async def enqueue(self, to_node: str, msg_type: str, body: dict,
                      session_id: str = None, reply_to: str = None,
                      system: bool = False, ttl_hours: float = 168):
        """Enqueue an item in the outbox for delivery."""
        # 1. Check outbox cap (5000)
        outbox_count = self._node.storage.count_outbox()
        if outbox_count >= 5000:
            self._log.warning(f"Outbox full ({outbox_count} items), rejecting mail to {to_node[:16]}")
            raise Exception("Outbox full")

        item_id = str(uuid.uuid4())
        timestamp = time.time()
        ttl_expires = timestamp + (ttl_hours * 3600)
        
        item = {
            "item_id": item_id,
            "from_node": self._node.node_info.node_id,
            "to_node": to_node,
            "timestamp": timestamp,
            "body": body,
            "session_id": session_id,
            "msg_type": msg_type,
            "reply_to": reply_to,
            "ttl_expires": ttl_expires,
            "system": 1 if system else 0
        }
        
        body_json = json.dumps(item)
        
        # Store in outbox via writer queue for serialization [R-01]
        await self._node._enqueue_write(
            self._node.storage.enqueue_outbox,
            item_id, to_node, body_json, ttl_expires
        )
        if self._debug:
            self._log.info(f"MAIL_ENQUEUE to={to_node[:16]} type={msg_type} id={item_id[:8]} system={system}")
        # v0.26.0: Track correspondent (sent)
        self._node.storage.upsert_correspondent(to_node, sent=True, received=False)
        # v0.17.5: Immediate flush — don't wait for next heartbeat tick
        asyncio.create_task(self.flush_outbox())
        return item_id

    async def push_to_peer(self, peer_node_id: str, peer_host: str, peer_port: int):
        """Check outbox for pending items to this peer, send MailSync if any."""
        # V17-012: Self-delivery short-circuit — skip network for mail to ourselves
        if peer_node_id == self._node.node_info.node_id:
            await self._self_deliver(peer_node_id)
            return
        # V17-005: Per-peer singleflight — skip if already pushing to this peer
        if peer_node_id in self._push_in_flight:
            return
        self._push_in_flight.add(peer_node_id)
        try:
            await self._push_to_peer_inner(peer_node_id, peer_host, peer_port)
        finally:
            self._push_in_flight.discard(peer_node_id)

    async def _self_deliver(self, node_id: str):
        """Deliver outbox items destined for ourselves directly to inbox."""
        pending = self._node.storage.get_pending_outbox(node_id, limit=50)
        if not pending:
            return
        item_ids = [item["item_id"] for item in pending]
        await self._node._enqueue_write(self._node.storage.mark_outbox_sending, item_ids)

        delivered = []
        for p in pending:
            item = json.loads(p["body_json"])
            item_id = item.get("item_id")
            if not item_id:
                continue
            msg_type = item.get("msg_type", "")
            is_system = msg_type in self._mail_handlers
            stored = await self._node._enqueue_write(
                self._node.storage.store_mail_from_sync,
                item_id, node_id, node_id,
                item.get("timestamp"), json.dumps(item.get("body")), item.get("session_id"),
                msg_type, item.get("reply_to"), item.get("ttl_expires"),
                is_system
            )
            if stored:
                self._fire_mail_received(item, node_id, node_id)
                if is_system:
                    asyncio.create_task(self._dispatch_system_item(item))
            delivered.append(item_id)

        if delivered:
            await self._node._enqueue_write(
                self._node.storage.mark_outbox_delivered_for_peer,
                delivered, node_id
            )
            if self._debug:
                self._log.info(f"MAIL_SELF_DELIVER count={len(delivered)}")

    async def _push_to_peer_inner(self, peer_node_id: str, peer_host: str, peer_port: int):
        """Inner push logic — always called under singleflight guard."""
        # 1. Get pending items (limit 50)
        pending = self._node.storage.get_pending_outbox(peer_node_id, limit=50)
        if not pending:
            return

        item_ids = [item["item_id"] for item in pending]
        
        # 2. Mark as sending
        await self._node._enqueue_write(self._node.storage.mark_outbox_sending, item_ids)

        # 3. Build MailSync message
        from nacl.public import SealedBox, PublicKey
        import base64
        items = []
        # Look up peer encryption key once outside the loop
        peer_key = self._node.storage.get_peer_encryption_key(peer_node_id)
        sealed_box = None
        if peer_key:
            try:
                sealed_box = SealedBox(PublicKey(bytes.fromhex(peer_key)))
            except Exception as e:
                self._log.warning(f"Failed to create SealedBox for {peer_node_id[:16]}: {e}")
        blocked_ids = []
        for p in pending:
            item = json.loads(p["body_json"])

            # Egress filter: check plaintext body before encryption
            body = item.get("body")
            if body:
                body_str = json.dumps(body) if isinstance(body, dict) else str(body)
                if not self._node._egress.check(body_str):
                    self._log.critical(f"EGRESS_FILTER_BLOCK mail item_id={p['item_id'][:8]} to={peer_node_id[:16]}")
                    # v0.33.0: security.egress_blocked
                    _bus = getattr(self._node, 'bus', None)
                    if _bus:
                        _bus.emit("security.egress_blocked", msg_type=item.get("msg_type", "mail"), target=peer_node_id)
                    blocked_ids.append(p["item_id"])
                    continue  # skip this item, don't send it

            # Opportunistic encryption
            if sealed_box is not None:
                try:
                    body_bytes = json.dumps(item["body"]).encode('utf-8')
                    encrypted = sealed_box.encrypt(body_bytes)
                    item["encrypted_body"] = base64.b64encode(encrypted).decode('utf-8')
                    item["body"] = "[encrypted]"
                except Exception as e:
                    self._log.warning(f"Failed to encrypt mail item {item.get('item_id')}: {e}")

            items.append(item)

        # Revert egress-blocked items from 'sending' back to 'pending'
        if blocked_ids:
            await self._node._enqueue_write(self._node.storage.mark_outbox_pending, blocked_ids)
            self._log.warning(f"EGRESS_BLOCK_REVERT count={len(blocked_ids)} to={peer_node_id[:16]}")

        if not items:
            return  # all items were blocked

        msg = self._node._sign(MailSync(
            sender_node_id=self._node.node_info.node_id,
            items=items,
            batch_seq=pending[-1]["batch_seq"] # Last one in batch
        ))
        
        if self._debug:
            self._log.info(f"MAIL_PUSH to={peer_node_id[:16]} items={len(items)} seq={pending[-1]['batch_seq']}")
        
        # 4. Send via pool and process MailAck response
        try:
            push_host, push_port = self._node.resolve_peer(peer_node_id, peer_host, peer_port)
            resp = await self._node._pool.send(peer_node_id, push_host, push_port, msg)
            if resp is None:
                self._log.warning(f"Failed to send MailSync to {peer_node_id[:16]}, reverting to pending")
                await self._node._enqueue_write(self._node.storage.mark_outbox_pending, item_ids)
                # v0.33.0: bus event for null-response delivery failure
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", batch_size=len(item_ids), error="no_response")
            elif isinstance(resp, MailAck) and resp.item_ids:
                # V17-002: Process ACK inline — transition sending→delivered
                await self._node._enqueue_write(
                    self._node.storage.mark_outbox_delivered_for_peer,
                    resp.item_ids, peer_node_id
                )
                if self._debug:
                    self._log.info(f"MAIL_ACK_RECV from={peer_node_id[:16]} confirmed={len(resp.item_ids)}")
            else:
                # Unexpected response type — revert to retry next cycle
                self._log.warning(f"Unexpected response from {peer_node_id[:16]}: {type(resp).__name__}")
                await self._node._enqueue_write(self._node.storage.mark_outbox_pending, item_ids)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", batch_size=len(item_ids), error=f"unexpected_{type(resp).__name__}")
        except Exception as e:
            self._log.error(f"Error during MailSync push to {peer_node_id[:16]}: {e}")
            # v0.33.0: mail.delivery_failed
            _bus = getattr(self._node, 'bus', None)
            if _bus:
                _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", batch_size=len(item_ids), error=str(e)[:200])
            await self._node._enqueue_write(self._node.storage.mark_outbox_pending, item_ids)

    async def handle_mail_sync(self, msg: MailSync, peer_ip: str):
        """Handle incoming MailSync — store items, dispatch system mail, send MailAck."""
        # V17-010: Reject oversized batches
        if len(msg.items) > 50:
            self._log.warning(f"MailSync from {msg.sender_node_id[:16]}: {len(msg.items)} items exceeds limit 50")
            return self._node._sign(MailAck(
                sender_node_id=self._node.node_info.node_id,
                ack_seq=msg.batch_seq,
                item_ids=[]
            ))

        # v0.17.0: Auto-populate address book cached tier
        # Since we have peer_ip here, use it to update address book
        await self._node._enqueue_write(
            self._node.storage.upsert_address,
            msg.sender_node_id, "cached", None,
            peer_ip
        )

        if self._debug:
            self._log.info(f"MAIL_RECV from={msg.sender_node_id[:16]} ip={peer_ip} items={len(msg.items)} seq={msg.batch_seq}")

        confirmed_ids = []
        from nacl.public import SealedBox
        import base64
        for item in msg.items:
            item_id = item.get("item_id")
            if not item_id: continue

            if "encrypted_body" in item:
                if hasattr(self._node, '_x25519_private') and self._node._x25519_private is not None:
                    try:
                        box = SealedBox(self._node._x25519_private)
                        raw = base64.b64decode(item["encrypted_body"])
                        decrypted = box.decrypt(raw)
                        item["body"] = json.loads(decrypted.decode('utf-8'))
                        if isinstance(item["body"], dict):
                            item["body"]["_encrypted"] = True
                    except Exception as e:
                        self._log.warning(f"Failed to decrypt mail item {item_id[:8]}: {e}")
                        confirmed_ids.append(item_id)
                        continue
                else:
                    self._log.warning(f"Mail item {item_id[:8]} encrypted, but node missing X25519 key")
                    confirmed_ids.append(item_id)
                    continue

            # V17-010: Per-item body size cap (48KB)
            body = item.get("body")
            if body and len(json.dumps(body)) > 49152:
                self._log.warning(f"MailSync: item {item_id[:8]} body exceeds 48KB, skipping")
                confirmed_ids.append(item_id)  # Ack so sender purges
                continue

            # Check TTL
            ttl_expires = item.get("ttl_expires", 0)
            if time.time() > ttl_expires:
                self._log.warning(f"MailSync: item {item_id[:8]} expired, skipping")
                confirmed_ids.append(item_id) # Ack so sender purges
                continue
            # SA-ML7: Clamp TTL to max 7 days (168 hours) to prevent storage exhaustion
            max_ttl = time.time() + 168 * 3600
            if ttl_expires > max_ttl:
                item["ttl_expires"] = max_ttl
                
            # v0.29.1: Only inbox capacity blocks incoming user mail
            # System/jobreport mail routes to separate buckets regardless
            msg_type = item.get("msg_type", "")
            is_system_item = msg_type in self._mail_handlers
            if not is_system_item and not msg_type.startswith("knarr/system/task_result"):
                mail_cfg = self._node._config.get("mail", {})
                max_inbox = int(mail_cfg.get("max_inbox", mail_cfg.get("max_messages", 10000)))
                inbox_count = self._node.storage.count_mail_inbox()
                if inbox_count >= max_inbox:
                    self._log.warning(f"SYNC_INBOX_FULL count={inbox_count} max={max_inbox} from={msg.sender_node_id[:16]}")
                    break
                
            # Check accept_from policy
            mail_cfg = self._node._config.get("mail", {})
            accept_from = mail_cfg.get("accept_from", "all")
            if accept_from == "none":
                break
            if accept_from == "whitelist":
                whitelist = mail_cfg.get("whitelist", [])
                if msg.sender_node_id not in whitelist:
                    self._log.warning(f"MailSync: {msg.sender_node_id[:16]} not in whitelist")
                    break
            if accept_from == "groups":
                accept_groups = set(mail_cfg.get("accept_groups", []))
                if not accept_groups:
                    self._log.warning(f"MailSync: accept_from=groups but no accept_groups configured")
                    break
                engine = getattr(self._node, '_group_engine', None)
                if engine:
                    caller_groups = set(engine.get_groups(msg.sender_node_id))
                    if not caller_groups.intersection(accept_groups):
                        self._log.warning(f"MailSync: {msg.sender_node_id[:16]} not in accepted groups")
                        break
                else:
                    self._log.warning(f"MailSync: group engine not available, rejecting")
                    break

            # System flag: dispatch if msg_type has a registered handler
            msg_type = item.get("msg_type", "")
            is_system = msg_type in self._mail_handlers
            
            # Store in inbox — force from_node to authenticated sender (anti-spoof)
            stored = await self._node._enqueue_write(
                self._node.storage.store_mail_from_sync,
                item_id, msg.sender_node_id, self._node.node_info.node_id,
                item.get("timestamp"), json.dumps(item.get("body")), item.get("session_id"),
                item.get("msg_type"), item.get("reply_to"), item.get("ttl_expires"),
                is_system
            )
            
            if stored:
                confirmed_ids.append(item_id)
                self._fire_mail_received(item, msg.sender_node_id, self._node.node_info.node_id)
                # v0.33.0: mail.received (push path)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.received", from_node=msg.sender_node_id[:16], msg_type=str(msg_type)[:64], session_id=str(item.get("session_id", ""))[:64], bucket="system" if is_system else "inbox")
                if self._debug:
                    self._log.info(f"MAIL_STORE id={item_id[:8]} type={item.get('msg_type','?')} from={msg.sender_node_id[:16]} system={is_system}")
                if is_system:
                    # Run system dispatch in background task
                    asyncio.create_task(self._dispatch_system_item(item))
            else:
                # Duplicate item_id
                confirmed_ids.append(item_id)
                if self._debug:
                    self._log.info(f"MAIL_DEDUP id={item_id[:8]} from={msg.sender_node_id[:16]}")

        # v0.26.0: Track correspondent (received)
        if confirmed_ids:
            self._node.storage.upsert_correspondent(msg.sender_node_id, sent=False, received=True)

        # M-014: Replicate referenced assets from sender's sidecar
        for item in msg.items:
            asyncio.create_task(self._sync_assets_from_mail(item, msg.sender_node_id))

        # Return MailAck
        if self._debug:
            self._log.info(f"MAIL_ACK_SEND to={msg.sender_node_id[:16]} confirmed={len(confirmed_ids)} seq={msg.batch_seq}")
        return self._node._sign(MailAck(
            sender_node_id=self._node.node_info.node_id,
            ack_seq=msg.batch_seq,
            item_ids=confirmed_ids
        ))

    async def handle_mail_ack(self, msg: MailAck):
        """Handle incoming MailAck — mark outbox items as delivered.
        V17-003: Binds ACK to sender identity — only marks items destined for this peer."""
        if not msg.item_ids:
            return

        await self._node._enqueue_write(
            self._node.storage.mark_outbox_delivered_for_peer,
            msg.item_ids, msg.sender_node_id
        )
        if self._debug:
            self._log.info(f"MAIL_ACK_HANDLE from={msg.sender_node_id[:16]} confirmed={len(msg.item_ids)}")

    async def flush_outbox(self):
        """Sweep outbox for pending recipients and push, even if not in peer table.

        Solves the chicken-and-egg problem where MailSync only pushes during
        heartbeat ticks to known peers, but a peer may not be in the routing
        table yet (e.g. same-LAN nodes that fail NAT hairpin discovery).
        Uses resolve_peer() which applies peer_overrides for address resolution.
        """
        recipients = self._node.storage.get_outbox_recipients()
        if not recipients:
            return
        for to_node in recipients:
            if to_node == self._node.node_info.node_id:
                await self._self_deliver(to_node)
                continue
            # Try peer table first
            peer_info = next((p for p in self._node.storage.get_peers() if p.node_id == to_node), None)
            if peer_info:
                h, p = self._node.resolve_peer(peer_info.node_id, peer_info.host, peer_info.port)
                await self.push_to_peer(to_node, h, p)
            else:
                # Not in peer table — try resolve_peer with dummy address (peer_override may resolve it)
                h, p = self._node.resolve_peer(to_node, "0.0.0.0", 0)
                if h != "0.0.0.0" and p != 0:
                    if self._debug:
                        self._log.info(f"MAIL_FLUSH_ORPHAN to={to_node[:16]} via override {h}:{p}")
                    await self.push_to_peer(to_node, h, p)
                else:
                    # Fall back to skill table address (gossip-discovered providers)
                    skill_addr = self._node.storage.get_provider_address(to_node)
                    if skill_addr:
                        sh, sp = skill_addr
                        if self._debug:
                            self._log.info(f"MAIL_FLUSH_SKILL to={to_node[:16]} via skill table {sh}:{sp}")
                        await self.push_to_peer(to_node, sh, sp)
                    else:
                        self._log.warning(f"MAIL_FLUSH_SKIP to={to_node[:16]} (not in peers, no override, no skill address)")

    async def cleanup(self):
        """Periodic cleanup: expire outbox items, purge delivered items."""
        now = time.time()
        expired = await self._node._enqueue_write(self._node.storage.purge_outbox_expired, now)
        if expired:
            self._log.info(f"MAIL_PURGE_EXPIRED count={expired}")
            _bus_exp = getattr(self._node, 'bus', None)
            if _bus_exp:
                _bus_exp.emit("mail.outbox_expired", count=expired)

        # Purge delivered items older than 1 hour
        cutoff = now - 3600
        purged = await self._node._enqueue_write(self._node.storage.purge_outbox_delivered, cutoff)
        if purged and self._debug:
            self._log.info(f"MAIL_PURGE_DELIVERED count={purged}")

        # V17-011: Evict stale cached addresses (keep 200 most recent)
        await self._node._enqueue_write(self._node.storage.evict_cached_addresses, 200)

        # S-05: Evict stale pull rate-limit entries (>5 min)
        mono_now = time.monotonic()
        stale = [k for k, v in self._pull_last_req.items() if mono_now - v > 300]
        for k in stale:
            del self._pull_last_req[k]

        # S-13: Evict stale correspondents (>30 days, cap 10k)
        await self._node._enqueue_write(self._node.storage.evict_stale_correspondents)

        # M-020: Recover items stuck in 'sending' state + bus events
        _bus = getattr(self._node, 'bus', None)
        try:
            reverted, failed = await self._node._enqueue_write(
                self._node.storage.revert_stale_sending, 120, 5
            )
            if reverted:
                self._log.warning(f"MAIL_STUCK_RECOVERED count={reverted}")
                if _bus:
                    _bus.emit("mail.outbox_stuck", recovered=reverted, failed=0, action="reverted")
            if failed:
                self._log.warning(f"MAIL_STUCK_FAILED count={failed} (exceeded max retries)")
                if _bus:
                    _bus.emit("mail.outbox_stuck", recovered=0, failed=failed, action="abandoned")
        except Exception:
            pass  # storage method may not exist on older DBs

        # v0.33.0: mail.inbox_stale — alert on old unread messages
        _bus = getattr(self._node, 'bus', None)
        if _bus:
            stale_hours = float(self._node._config.get("mail", {}).get("stale_inbox_hours", 24))
            stale_cutoff = now - (stale_hours * 3600)
            try:
                stale_msgs = self._node.storage.get_stale_inbox_messages(stale_cutoff, limit=10)
                for sm in stale_msgs:
                    _mid = sm.get("item_id", "")
                    if _mid in self._notified_stale:
                        continue  # already alerted
                    _bus.emit("mail.inbox_stale", from_node=(sm.get("from_node") or "")[:16], message_id=_mid,
                              age_seconds=int(now - sm.get("timestamp", now)), bucket="inbox")
                    self._notified_stale.add(_mid)
                # Cap dedup set at 1000 to prevent unbounded growth
                if len(self._notified_stale) > 1000:
                    self._notified_stale = set(list(self._notified_stale)[-500:])
            except Exception:
                pass  # storage may not support this yet

    async def _sync_assets_from_mail(self, item: dict, sender_node_id: str):
        """M-014: Replicate sidecar assets referenced in mail to local node."""
        import os
        asset_dir = getattr(self._node, '_asset_dir', '')
        if not asset_dir:
            return

        # Collect asset hashes from spillover + attachment URIs
        hashes = set()
        body = item.get("body")
        if isinstance(body, dict):
            metadata = body.get("metadata") or {}
            spill = metadata.get("_spillover_hash")
            if spill and isinstance(spill, str) and len(spill) == 64:
                hashes.add(spill)
            for att in (body.get("attachments") or []):
                uri = att.get("uri", "") if isinstance(att, dict) else str(att)
                if uri.startswith("knarr-asset://"):
                    h = uri[len("knarr-asset://"):]
                    if len(h) == 64 and all(c in '0123456789abcdef' for c in h):
                        hashes.add(h)

        if not hashes:
            return

        # Skip assets already stored locally
        hashes = {h for h in hashes if not os.path.isfile(os.path.join(asset_dir, h))}
        if not hashes:
            return

        # Look up sender sidecar address
        addr = self._node.storage.get_address(sender_node_id)
        if not addr or not addr.get("sidecar_port"):
            if self._debug:
                self._log.info(f"ASSET_SYNC_SKIP no sidecar for {sender_node_id[:16]}")
            return

        host = addr["last_ip"]
        port = int(addr["sidecar_port"])
        loop = asyncio.get_running_loop()

        for h in hashes:
            try:
                await loop.run_in_executor(
                    self._node._handler_pool,
                    self._node._fetch_sidecar_asset, host, port, h)
            except Exception as e:
                self._log.warning(f"ASSET_SYNC_FAIL hash={h[:16]} from={sender_node_id[:16]}: {e}")

    def _fire_mail_received(self, item: dict, from_node: str, to_node: str):
        """M-016: Fire on_mail_received plugin hook (fire-and-forget)."""
        if self._plugins and self._plugins.plugins:
            asyncio.create_task(self._plugins.on_mail_received(
                item.get("msg_type", ""),
                from_node, to_node,
                item.get("body"),
                item.get("session_id"),
            ))

    async def _dispatch_system_item(self, item: dict):
        """Dispatch a system mail item to its registered handler."""
        msg_type = item.get("msg_type", "")
        handler = self._mail_handlers.get(msg_type)
        if handler:
            try:
                if self._debug:
                    self._log.info(f"MAIL_DISPATCH type={msg_type} from={item.get('from_node', '?')[:16]}")
                await handler(item)
            except Exception:
                self._log.error(f"System mail handler failed for {msg_type}", exc_info=True)
        else:
            self._log.warning(f"No handler for system mail type: {msg_type}")

    # ── Tier 2: Mail Pull ──────────────────────────────────────────

    async def handle_mail_pull_req(self, msg: MailPullReq, peer_ip: str) -> MailPullResp:
        """Handle incoming pull request — return pending outbox items for requester."""
        requester = msg.requester_node_id
        max_batch = self._node._config.get("mail", {}).get("max_pull_batch", 5)

        # Rate limit: 1 pull per node per 60s
        now = time.monotonic()
        last = self._pull_last_req.get(requester, 0)
        if now - last < 60.0:
            if self._debug:
                self._log.info(f"MAIL_PULL_RATE_LIMIT requester={requester[:16]}")
            return MailPullResp(
                sender_node_id=self._node.node_info.node_id,
                items=[]
            )
        self._pull_last_req[requester] = now

        # Get pending items addressed TO the requester
        pending = self._node.storage.get_pending_outbox_for_requester(requester, limit=max_batch)
        items = []
        for p in pending:
            try:
                body = json.loads(p["body_json"])
                items.append(body)
            except (json.JSONDecodeError, KeyError):
                continue

        if self._debug:
            self._log.info(f"MAIL_PULL_RESP requester={requester[:16]} items={len(items)}")

        return MailPullResp(
            sender_node_id=self._node.node_info.node_id,
            items=items
        )

    async def handle_mail_pull_ack(self, msg: MailPullAck):
        """Handle pull ACK — mark outbox items as delivered via pull."""
        if not msg.item_ids:
            return
        await self._node._enqueue_write(
            self._node.storage.mark_outbox_pull_delivered,
            msg.item_ids, msg.requester_node_id
        )
        if self._debug:
            self._log.info(f"MAIL_PULL_ACK requester={msg.requester_node_id[:16]} confirmed={len(msg.item_ids)}")

    async def pull_from_correspondents(self):
        """Pull pending mail from known correspondents. Exponential backoff."""
        correspondents = self._node.storage.get_correspondents(limit=10)
        delay = 0.0
        pulled = 0
        for i, corr in enumerate(correspondents):
            if i >= 5:
                break  # pull storm mitigation: max 5 correspondents
            if delay > 0:
                await asyncio.sleep(delay)
            count = await self._pull_from_peer(corr["node_id"])
            pulled += count
            delay = min(delay * 2 if delay > 0 else 2.0, 16.0)  # 0, 2, 4, 8, 16
        if pulled and self._debug:
            self._log.info(f"MAIL_PULL_SWEEP total={pulled}")

    async def _pull_from_peer(self, peer_node_id: str) -> int:
        """Send MAIL_PULL_REQ to a specific peer. Returns count of items received."""
        # C-13: Skip peers that don't support mail pull (pre-v0.26.0)
        peer_info = next((p for p in self._node.storage.get_peers() if p.node_id == peer_node_id), None)
        if peer_info and hasattr(peer_info, 'version') and peer_info.version:
            try:
                parts = peer_info.version.split(".")
                if len(parts) >= 2 and (int(parts[0]), int(parts[1])) < (0, 26):
                    return 0
            except (ValueError, IndexError):
                pass  # unknown version format, try anyway

        # Resolve peer address
        if peer_info:
            h, p = self._node.resolve_peer(peer_node_id, peer_info.host, peer_info.port)
        else:
            h, p = self._node.resolve_peer(peer_node_id, "0.0.0.0", 0)
            if h == "0.0.0.0" and p == 0:
                # Try skill table address
                skill_addr = self._node.storage.get_provider_address(peer_node_id)
                if skill_addr:
                    h, p = skill_addr
                else:
                    return 0

        # Build and send pull request
        req = self._node._sign(MailPullReq(
            requester_node_id=self._node.node_info.node_id
        ))

        try:
            resp = await self._node._pool.send(peer_node_id, h, p, req)
        except Exception as e:
            self._log.warning(f"MAIL_PULL_FAIL peer={peer_node_id[:16]}: {e}")
            return 0

        if not isinstance(resp, MailPullResp) or not resp.items:
            return 0

        # Store pulled items in inbox (same logic as handle_mail_sync)
        confirmed_ids = []
        for item in resp.items:
            item_id = item.get("item_id")
            if not item_id:
                continue

            # TTL check
            ttl_expires = item.get("ttl_expires", 0)
            if time.time() > ttl_expires:
                confirmed_ids.append(item_id)
                continue

            # Body size cap (48KB)
            body = item.get("body")
            if body and len(json.dumps(body)) > 49152:
                confirmed_ids.append(item_id)
                continue

            # Store
            msg_type = item.get("msg_type", "")
            is_system = msg_type in self._mail_handlers
            stored = await self._node._enqueue_write(
                self._node.storage.store_mail_from_sync,
                item_id, peer_node_id, self._node.node_info.node_id,
                item.get("timestamp"), json.dumps(item.get("body")), item.get("session_id"),
                msg_type, item.get("reply_to"), ttl_expires,
                is_system
            )
            confirmed_ids.append(item_id)
            if stored:
                self._fire_mail_received(item, peer_node_id, self._node.node_info.node_id)
                # v0.33.0: mail.received (pull path)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.received", from_node=peer_node_id[:16], msg_type=str(msg_type)[:64], session_id=str(item.get("session_id", ""))[:64], bucket="system" if is_system else "inbox")
                if is_system:
                    asyncio.create_task(self._dispatch_system_item(item))

        # M-014: Replicate referenced assets from sender's sidecar
        for item in resp.items:
            asyncio.create_task(self._sync_assets_from_mail(item, peer_node_id))

        # Track correspondent once per pull (C-15: was inside loop)
        if confirmed_ids:
            self._node.storage.upsert_correspondent(peer_node_id, sent=False, received=True)

        # Send ACK
        if confirmed_ids:
            ack = self._node._sign(MailPullAck(
                requester_node_id=self._node.node_info.node_id,
                item_ids=confirmed_ids
            ))
            try:
                await self._node._pool.send(peer_node_id, h, p, ack)
            except Exception:
                pass  # Best effort — items will be re-pulled next cycle

        if self._debug:
            self._log.info(f"MAIL_PULL_RECV peer={peer_node_id[:16]} items={len(confirmed_ids)}")
        return len(confirmed_ids)
