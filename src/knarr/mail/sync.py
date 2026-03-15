import asyncio
import logging
import time
import json
import uuid
from typing import List, Dict, Any, Optional, Callable
from ..core.messages import MailSync, MailAck, MailPullReq, MailPullResp, MailPullAck

logger = logging.getLogger("knarr.mail.sync")

# BR-GATE-001: commerce message types that are generated locally and carry no proof field.
# WM auto_approve already covers these in wm-review.toml — these types bypass Gate 1/2
# because they are not received from untrusted external sources; they are enqueued by
# the local node itself (credit accounting path). Signed commerce documents (settlements,
# credit_notes) are NOT in this set and always pass through WM normally.
_TRUSTED_UNSIGNED_COMMERCE = {"knarr/commerce/tab_reminder"}

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
        self._receipt_skip_warned = False  # FIX-10: warn once if _write_receipt missing
        # M-018: Per-peer delivery state tracking
        self._peer_delivery_state: Dict[str, Dict[str, Any]] = {}  # node_id -> {last_attempt, consecutive_failures, next_retry_after, circuit_open}

        # v0.39.0 C3: Mail circuit breaker — per-peer failure tracking
        self._circuit_state: Dict[str, Dict[str, Any]] = {}
        self._circuit_backoff_steps = [30, 60, 120, 300]  # seconds
        self._circuit_threshold = 3  # failures before circuit opens

    def _circuit_allows(self, peer: str) -> bool:
        """Check if circuit breaker allows sending to this peer."""
        state = self._circuit_state.get(peer)
        if not state or not state.get("open", False):
            return True
        # Check if backoff period has elapsed (half-open)
        if time.monotonic() >= state.get("retry_after", 0):
            return True
        return False

    def _circuit_on_success(self, peer: str):
        """Reset circuit breaker on successful delivery."""
        if peer in self._circuit_state:
            del self._circuit_state[peer]

    def _circuit_on_failure(self, peer: str):
        """Record failure, potentially open circuit."""
        state = self._circuit_state.get(peer)
        if not state:
            state = {"failures": 0, "open": False, "retry_after": 0, "backoff_index": 0}
            self._circuit_state[peer] = state
        state["failures"] = state.get("failures", 0) + 1
        if state["failures"] >= self._circuit_threshold:
            state["open"] = True
            idx = min(state.get("backoff_index", 0), len(self._circuit_backoff_steps) - 1)
            backoff = self._circuit_backoff_steps[idx]
            state["retry_after"] = time.monotonic() + backoff
            state["backoff_index"] = min(idx + 1, len(self._circuit_backoff_steps) - 1)
            self._log.warning(
                f"CIRCUIT_OPEN peer={peer[:16]} failures={state['failures']} backoff={backoff}s"
            )
            bus = getattr(self._node, 'bus', None)
            if bus:
                bus.emit("mail.peer_circuit_open", peer=peer, failures=state["failures"],
                         backoff_seconds=backoff, identity=peer)

    def register_handler(self, msg_type: str, handler: Callable):
        """Register a dispatch handler for a system mail msg_type."""
        self._mail_handlers[msg_type] = handler

    async def _run_mail_admission(self, sender_node_id: str, sender_key: str):
        import hashlib
        from ..commerce.admission_pipeline import AdmissionContext, run_admission

        skill_name = "knarr-mail"
        initial_credit, min_balance = self._node._resolve_policy(sender_key, skill_name)
        initial_trust = self._node._get_initial_trust(sender_node_id)
        entry = await self._node._enqueue_write(
            self._node.storage.get_or_create_ledger_entry,
            sender_key, initial_credit, initial_trust
        )

        peer_nid = hashlib.sha256(bytes.fromhex(sender_key)).hexdigest()
        group_engine = getattr(self._node, "_group_engine", None)
        meter = await self._node._enqueue_write(
            self._node.storage.meter_get, peer_nid, skill_name, ""
        )
        meter_count = meter["count"] if meter else 0
        skill_sheet = getattr(self._node, "_own_skills", {}).get(skill_name)
        skill_cfg = self._node._get_skill_runtime_config(skill_name)

        result = run_admission(AdmissionContext(
            caller_key=sender_key,
            skill_name=skill_name,
            base_price=skill_sheet.price if skill_sheet else float(self._node._config.get("mail", {}).get("price", 1.0)),
            balance=entry.balance,
            soft_limit=min_balance,
            hard_limit=min_balance,
            tit_for_tat=self._node.policy.tit_for_tat,
            peer_node_id=peer_nid,
            peer_groups=set(group_engine.get_groups(peer_nid)) if group_engine else set(),
            discount_rules=self._node._load_discount_rules(peer_nid, skill_name),
            cost_projection=self._node._get_cost_projection(skill_name),
            pricing_config=self._node._build_pricing_config(skill_name),
            prepaid_balance=getattr(entry, "prepaid", 0.0),
            meter_count=meter_count,
            meter_max_count=int(skill_cfg.get("meter_max_count", 0)),
            identity=self._node.node_info.node_id,
            counterparty=peer_nid,
        ))
        return result, entry, initial_credit, min_balance

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
        # v0.39.0 C3: Circuit breaker check — skip if circuit is open
        if not self._circuit_allows(peer_node_id):
            if self._debug:
                self._log.debug(f"CIRCUIT_SKIP peer={peer_node_id[:16]} — circuit open")
            return
        # V17-005: Per-peer singleflight — skip if already pushing to this peer
        if peer_node_id in self._push_in_flight:
            return
        self._push_in_flight.add(peer_node_id)
        try:
            await self._push_to_peer_inner(peer_node_id, peer_host, peer_port)
            self._circuit_on_success(peer_node_id)
        except Exception:
            self._circuit_on_failure(peer_node_id)
            raise
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
                # B4: mail_receive_receipt (stored) — self-delivery path
                if hasattr(self._node, '_write_receipt'):
                    import hashlib as _hashlib_self
                    _body_self = item.get("body")
                    try:
                        _body_str_self = json.dumps(_body_self, sort_keys=True, separators=(',',':')) if isinstance(_body_self, (dict, list)) else str(_body_self) if _body_self is not None else ""
                    except (TypeError, ValueError):
                        _body_str_self = str(_body_self) if _body_self is not None else ""
                    _ph_self = "sha256:" + _hashlib_self.sha256(_body_str_self.encode("utf-8")).hexdigest()
                    _pb_self = len(_body_str_self.encode("utf-8"))
                    self._node._write_receipt(
                        document_type="mail_receive_receipt",
                        payload={
                            "receiver": self._node.node_info.node_id,
                            "sender": node_id,
                            "message_id": item_id,
                            "message_type": msg_type,
                            "receipt": {
                                "status": "stored",
                                "payload_bytes": _pb_self,
                                "payload_hash": _ph_self,
                            },
                        },
                        counterparty=node_id,
                        order_ref=item_id,
                        proof_purpose="acknowledgment",
                        sign=True,
                    )
                elif not self._receipt_skip_warned:
                    self._log.warning("RECEIPT_SKIP: node missing _write_receipt — mail receipts disabled")
                    self._receipt_skip_warned = True
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
        """Inner push logic — always called under singleflight guard.
        
        M-018: Implements exponential backoff and circuit breaker for dead peers.
        """
        # M-018: Check circuit breaker and backoff before attempting delivery
        now = time.time()
        state = self._peer_delivery_state.get(peer_node_id)
        if state and state.get("circuit_open"):
            if now < state.get("next_retry_after", 0):
                self._log.warning(f"Circuit open for peer {peer_node_id[:16]}, next retry at {state['next_retry_after']}")
                return
            # Circuit cooldown expired, allow one retry attempt
            state["circuit_open"] = False
        
        # M-018: Check backoff timing
        if state and state.get("next_retry_after") and now < state["next_retry_after"]:
            return  # Still in backoff period
        
        # 1. Get pending items (limit 50)
        pending = self._node.storage.get_pending_outbox(peer_node_id, limit=50)
        if not pending:
            return

        item_ids = [item["item_id"] for item in pending]
        batch_start = now
        consecutive_failures = state.get("consecutive_failures", 0) if state else 0

        # 2. Mark as sending
        await self._node._enqueue_write(self._node.storage.mark_outbox_sending, item_ids)

        # 3. Build MailSync message
        from nacl.public import SealedBox, PublicKey
        import base64
        items = []
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
            body = item.get("body")
            if body:
                body_str = json.dumps(body) if isinstance(body, dict) else str(body)
                if not self._node._egress.check(body_str):
                    self._log.critical(f"EGRESS_FILTER_BLOCK mail item_id={p['item_id'][:8]} to={peer_node_id[:16]}")
                    _bus = getattr(self._node, 'bus', None)
                    if _bus:
                        _bus.emit("security.egress_blocked", msg_type=item.get("msg_type", "mail"), target=peer_node_id, identity=self._node.node_info.node_id)
                    blocked_ids.append(p["item_id"])
                    continue

            if sealed_box is not None:
                try:
                    body_bytes = json.dumps(item["body"]).encode('utf-8')
                    encrypted = sealed_box.encrypt(body_bytes)
                    item["encrypted_body"] = base64.b64encode(encrypted).decode('utf-8')
                    item["body"] = "[encrypted]"
                except Exception as e:
                    self._log.warning(f"Failed to encrypt mail item {item.get('item_id')}: {e}")

            items.append(item)

        if blocked_ids:
            await self._node._enqueue_write(self._node.storage.mark_outbox_pending, blocked_ids)
            self._log.warning(f"EGRESS_BLOCK_REVERT count={len(blocked_ids)} to={peer_node_id[:16]}")

        if not items:
            return

        msg = self._node._sign(MailSync(
            sender_node_id=self._node.node_info.node_id,
            items=items,
            batch_seq=pending[-1]["batch_seq"]
        ))

        if self._debug:
            self._log.info(f"MAIL_PUSH to={peer_node_id[:16]} items={len(items)} seq={pending[-1]['batch_seq']}")

        # 4. Send via pool and process MailAck response
        delivery_success = False
        error_string = None
        delivered_ids = []
        push_host, push_port = self._node.resolve_peer(peer_node_id, peer_host, peer_port)
        
        try:
            resp = await self._node._pool.send(peer_node_id, push_host, push_port, msg)
            if resp is None:
                error_string = "no_response"
                await self._node._enqueue_write(self._node.storage.mark_outbox_pending, item_ids)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", batch_size=len(item_ids), error="no_response", identity=self._node.node_info.node_id)
            elif isinstance(resp, MailAck) and resp.item_ids:
                delivery_success = True
                delivered_ids = resp.item_ids
                await self._node._enqueue_write(
                    self._node.storage.mark_outbox_delivered_for_peer,
                    resp.item_ids, peer_node_id
                )
                if self._debug:
                    self._log.info(f"MAIL_ACK_RECV from={peer_node_id[:16]} confirmed={len(resp.item_ids)}")
            else:
                error_string = f"unexpected_{type(resp).__name__}"
                await self._node._enqueue_write(self._node.storage.mark_outbox_pending, item_ids)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", batch_size=len(item_ids), error=error_string, identity=self._node.node_info.node_id)
        except Exception as e:
            error_string = str(e)[:200]
            self._log.error(f"Error during MailSync push to {peer_node_id[:16]}: {e}")
            _bus = getattr(self._node, 'bus', None)
            if _bus:
                _bus.emit("mail.delivery_failed", to_node=peer_node_id, message_id=item_ids[0] if item_ids else "", batch_size=len(item_ids), error=error_string, identity=self._node.node_info.node_id)
            await self._node._enqueue_write(self._node.storage.mark_outbox_pending, item_ids)

        # M-018: Update delivery state and write receipt
        elapsed_ms = int((time.time() - batch_start) * 1000)
        attempt = consecutive_failures + 1  # 1-indexed: first attempt = 1

        # Initialize or update state
        if peer_node_id not in self._peer_delivery_state:
            self._peer_delivery_state[peer_node_id] = {
                "last_attempt": now,
                "consecutive_failures": 0,
                "next_retry_after": None,
                "circuit_open": False
            }
        state = self._peer_delivery_state[peer_node_id]

        if delivery_success:
            # Reset on success
            state["consecutive_failures"] = 0
            state["next_retry_after"] = None
            state["circuit_open"] = False
            state["last_attempt"] = now
        else:
            # Increment failures and compute backoff
            state["consecutive_failures"] = consecutive_failures + 1
            state["last_attempt"] = now
            # Exponential backoff: 30s, 60s, 120s, 300s, 600s (cap at 10 min)
            backoff_schedule = [30, 60, 120, 300, 600]
            backoff_seconds = backoff_schedule[min(state["consecutive_failures"] - 1, 4)]
            state["next_retry_after"] = now + backoff_seconds

            # Circuit breaker: open after 5 consecutive failures
            if state["consecutive_failures"] >= 5:
                state["circuit_open"] = True
                self._log.warning(f"Circuit breaker OPEN for peer {peer_node_id[:16]} after {state['consecutive_failures']} failures")

        # B4: mail_delivery_receipt via centralized node._write_receipt()
        if delivery_success:
            _delivery_status = "ack"
        elif state.get("circuit_open"):
            _delivery_status = "term"
        else:
            _delivery_status = "nak"
        _delivery_payload = {
            "sender": self._node.node_info.node_id,
            "recipient": peer_node_id,
            "batch": {
                "message_ids": item_ids,
                "message_count": len(item_ids),
            },
            "delivery": {
                "status": _delivery_status,
                "attempt": attempt,
                "endpoint": f"{peer_node_id[:8]}@tcp",
                "duration_ms": elapsed_ms,
            },
        }
        if delivery_success:
            _delivery_payload["delivery"]["ack_item_ids"] = delivered_ids
        else:
            _delivery_payload["delivery"]["error"] = error_string
        if hasattr(self._node, '_write_receipt'):
            self._node._write_receipt(
                document_type="mail_delivery_receipt",
                payload=_delivery_payload,
                counterparty=peer_node_id,
                order_ref=item_ids[0] if item_ids else None,
                proof_purpose="acknowledgment",
                sign=True,
            )
        elif not self._receipt_skip_warned:
            self._log.warning("RECEIPT_SKIP: node missing _write_receipt — mail receipts disabled")
            self._receipt_skip_warned = True

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

            # System flag: only knarr/-prefixed msg_types are system mail
            msg_type = item.get("msg_type", "")
            is_system = msg_type.startswith("knarr/")
            if not is_system:
                sender_key = getattr(msg, "public_key", "")
                if not sender_key:
                    self._log.warning("MAIL_REJECT: empty public_key on push path, item=%s", item_id[:8])
                    confirmed_ids.append(item_id)
                    continue
                try:
                    result, entry, initial_credit, min_balance = await self._run_mail_admission(
                        msg.sender_node_id, sender_key
                    )
                except ValueError:
                    self._log.warning("MAIL_REJECT: invalid hex public_key on push path, item=%s", item_id[:8])
                    confirmed_ids.append(item_id)
                    continue
                if result.gate.outcome == "hard_block":
                    self._log.warning(
                        "MAIL_HARD_BLOCK from=%s item=%s balance=%.2f limit=%.2f",
                        msg.sender_node_id[:16], item_id[:8],
                        entry.balance, min_balance,
                    )
                    if getattr(self._node, "bus", None):
                        self._node.bus.emit(
                            "credit.sanctioned",
                            counterparty=sender_key,
                            limit_type="hard",
                            identity=sender_key,
                        )
                    confirmed_ids.append(item_id)
                    continue
            
            # Store in inbox — force from_node to authenticated sender (anti-spoof)
            stored = await self._node._enqueue_write(
                self._node.storage.store_mail_from_sync,
                item_id, msg.sender_node_id, self._node.node_info.node_id,
                item.get("timestamp"), json.dumps(item.get("body")), item.get("session_id"),
                item.get("msg_type"), item.get("reply_to"), item.get("ttl_expires"),
                is_system
            )
            
            if stored:
                if not is_system:
                    await self._node._enqueue_write(
                        self._node.storage.update_ledger_provider,
                        msg.public_key, max(0.0, result.pricing.final_price)
                    )
                    await self._node._maybe_send_tab_reminder(
                        msg.public_key,
                        result.gate.balance_after if result.gate.balance_after is not None else entry.balance,
                        initial_credit,
                        min_balance,
                    )
                confirmed_ids.append(item_id)
                self._fire_mail_received(item, msg.sender_node_id, self._node.node_info.node_id)
                # v0.33.0: mail.received (push path)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.received", from_node=msg.sender_node_id[:16], msg_type=str(msg_type)[:64], session_id=str(item.get("session_id", ""))[:64], bucket="system" if is_system else "inbox", identity=msg.sender_node_id)
                # B4: mail_receive_receipt (stored) — local record that mail arrived
                if hasattr(self._node, '_write_receipt'):
                    import hashlib as _hashlib
                    try:
                        _body_str = json.dumps(item.get("body"), sort_keys=True, separators=(',',':')) if isinstance(item.get("body"), (dict, list)) else str(item.get("body")) if item.get("body") is not None else ""
                    except (TypeError, ValueError):
                        _body_str = str(item.get("body")) if item.get("body") is not None else ""
                    _payload_hash = "sha256:" + _hashlib.sha256(_body_str.encode("utf-8")).hexdigest()
                    _payload_bytes = len(_body_str.encode("utf-8"))
                    self._node._write_receipt(
                        document_type="mail_receive_receipt",
                        payload={
                            "receiver": self._node.node_info.node_id,
                            "sender": msg.sender_node_id,
                            "message_id": item_id,
                            "message_type": msg_type,
                            "receipt": {
                                "status": "stored",
                                "payload_bytes": _payload_bytes,
                                "payload_hash": _payload_hash,
                            },
                        },
                        counterparty=msg.sender_node_id,
                        order_ref=item_id,
                        proof_purpose="acknowledgment",
                        sign=True,
                    )
                elif not self._receipt_skip_warned:
                    self._log.warning("RECEIPT_SKIP: node missing _write_receipt — mail receipts disabled")
                    self._receipt_skip_warned = True
                if self._debug:
                    self._log.info(f"MAIL_STORE id={item_id[:8]} type={item.get('msg_type','?')} from={msg.sender_node_id[:16]} system={is_system}")
                if is_system:
                    # Run system dispatch in background task
                    item["_sender_pubkey"] = getattr(msg, "public_key", "")
                    asyncio.create_task(self._dispatch_system_item(item))
            else:
                # Duplicate item_id — still confirm to sender
                confirmed_ids.append(item_id)
                # B4: mail_receive_receipt (duplicate) — dedup record
                if hasattr(self._node, '_write_receipt'):
                    self._node._write_receipt(
                        document_type="mail_receive_receipt",
                        payload={
                            "receiver": self._node.node_info.node_id,
                            "sender": msg.sender_node_id,
                            "message_id": item_id,
                            "message_type": item.get("msg_type", ""),
                            "receipt": {
                                "status": "duplicate",
                                "payload_bytes": 0,
                                "payload_hash": None,
                            },
                        },
                        counterparty=msg.sender_node_id,
                        order_ref=item_id,
                        proof_purpose="acknowledgment",
                        sign=True,
                    )
                elif not self._receipt_skip_warned:
                    self._log.warning("RECEIPT_SKIP: node missing _write_receipt — mail receipts disabled")
                    self._receipt_skip_warned = True
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
                        _bus = getattr(self._node, 'bus', None)
                        if _bus:
                            _bus.emit("mail.flush_skip", to_node=to_node[:16])

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
                    _bus.emit("mail.outbox_stuck", recovered=reverted, failed=0, action="reverted", identity=self._node.node_info.node_id)
            if failed:
                self._log.warning(f"MAIL_STUCK_FAILED count={failed} (exceeded max retries)")
                if _bus:
                    _bus.emit("mail.outbox_stuck", recovered=0, failed=failed, action="abandoned", identity=self._node.node_info.node_id)
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
                              age_seconds=int(now - sm.get("timestamp", now)), bucket="inbox", identity=self._node.node_info.node_id)
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
                dispatch_item = item
                # v0.42.0 A3: WM dispatch gate — documents with proof fields
                body = item.get("body", {})
                if isinstance(body, dict):
                    # Unwrap nested document from settlement/commerce envelopes
                    if msg_type.startswith("knarr/commerce/"):
                        wm_document = body.get("document", body)
                    else:
                        wm_document = body
                    # Commerce types are documents — always route through WM.
                    # Non-commerce system messages (task results, asset fetch)
                    # only go through WM if they carry a proof field; unsigned
                    # system messages are authenticated at the transport layer.
                    if not msg_type.startswith("knarr/commerce/"):
                        if not isinstance(wm_document, dict) or "proof" not in wm_document:
                            await handler(dispatch_item)
                            return
                    # BR-GATE-001: trusted unsigned commerce types bypass Gate 1/2.
                    # These are locally-generated notifications with no proof field;
                    # WM auto_approve covers them in wm-review.toml but Gate 1 rejects
                    # before reaching the review rule (empty sender pubkey on self-delivery,
                    # missing proof on cross-node path).
                    if msg_type in _TRUSTED_UNSIGNED_COMMERCE:
                        if not isinstance(wm_document, dict) or "proof" not in wm_document:
                            await handler(dispatch_item)
                            return
                    originator_pubkey = b""
                    sender_pubkey = item.get("_sender_pubkey", "")
                    if isinstance(sender_pubkey, str) and sender_pubkey:
                        try:
                            originator_pubkey = bytes.fromhex(sender_pubkey)
                        except ValueError:
                            self._log.warning("MAIL_REJECT: invalid _sender_pubkey on dispatch path, type=%s", msg_type)
                            return  # Fix #9: reject on corrupt sender identity
                    # Strip transport metadata before WM ingest — fields injected
                    # during decryption (e.g. _encrypted) were not present when
                    # sign_document ran, so they break JCS canonical form at
                    # verification time → Gate 1 signature mismatch.
                    wm_document = {k: v for k, v in wm_document.items() if not k.startswith("_")}
                    result = await self._node._wm_ingest(wm_document, originator_pubkey)
                    if result is not None:
                        if result.status == "held":
                            self._log.info("WM_HELD type=%s reason=%s", result.document_type, result.reason or "review")
                            return
                        if result.status == "rejected":
                            self._log.warning("WM_REJECTED type=%s reason=%s", result.document_type, result.reason or "rejected")
                            return
                        if result.document is not None:
                            dispatch_item = dict(item)
                            if msg_type.startswith("knarr/commerce/") and "document" in body:
                                dispatch_body = dict(body)
                                dispatch_body["document"] = result.document
                                dispatch_item["body"] = dispatch_body
                            else:
                                dispatch_item["body"] = result.document
                await handler(dispatch_item)
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
            is_system = msg_type.startswith("knarr/")
            if not is_system:
                sender_key = getattr(resp, "public_key", "")
                if not sender_key:
                    self._log.warning("MAIL_REJECT: empty public_key on pull path, item=%s", item_id[:8])
                    confirmed_ids.append(item_id)
                    continue
                try:
                    result, entry, initial_credit, min_balance = await self._run_mail_admission(
                        peer_node_id, sender_key
                    )
                except ValueError:
                    self._log.warning("MAIL_REJECT: invalid hex public_key on pull path, item=%s", item_id[:8])
                    confirmed_ids.append(item_id)
                    continue
                if result.gate.outcome == "hard_block":
                    self._log.warning(
                        "MAIL_HARD_BLOCK from=%s item=%s balance=%.2f limit=%.2f",
                        peer_node_id[:16], item_id[:8],
                        entry.balance, min_balance,
                    )
                    if getattr(self._node, "bus", None):
                        self._node.bus.emit(
                            "credit.sanctioned",
                            counterparty=sender_key,
                            limit_type="hard",
                            identity=sender_key,
                        )
                    confirmed_ids.append(item_id)
                    continue
            stored = await self._node._enqueue_write(
                self._node.storage.store_mail_from_sync,
                item_id, peer_node_id, self._node.node_info.node_id,
                item.get("timestamp"), json.dumps(item.get("body")), item.get("session_id"),
                msg_type, item.get("reply_to"), ttl_expires,
                is_system
            )
            confirmed_ids.append(item_id)
            if stored:
                if not is_system:
                    await self._node._enqueue_write(
                        self._node.storage.update_ledger_provider,
                        resp.public_key, max(0.0, result.pricing.final_price)
                    )
                    await self._node._maybe_send_tab_reminder(
                        resp.public_key,
                        result.gate.balance_after if result.gate.balance_after is not None else entry.balance,
                        initial_credit,
                        min_balance,
                    )
                self._fire_mail_received(item, peer_node_id, self._node.node_info.node_id)
                # v0.33.0: mail.received (pull path)
                _bus = getattr(self._node, 'bus', None)
                if _bus:
                    _bus.emit("mail.received", from_node=peer_node_id[:16], msg_type=str(msg_type)[:64], session_id=str(item.get("session_id", ""))[:64], bucket="system" if is_system else "inbox", identity=peer_node_id)
                # B4: mail_receive_receipt (stored) — pull path
                if hasattr(self._node, '_write_receipt'):
                    import hashlib as _hashlib_pull
                    _body_pull = item.get("body")
                    try:
                        _body_str_pull = json.dumps(_body_pull, sort_keys=True, separators=(',',':')) if isinstance(_body_pull, (dict, list)) else str(_body_pull) if _body_pull is not None else ""
                    except (TypeError, ValueError):
                        _body_str_pull = str(_body_pull) if _body_pull is not None else ""
                    _ph_pull = "sha256:" + _hashlib_pull.sha256(_body_str_pull.encode("utf-8")).hexdigest()
                    _pb_pull = len(_body_str_pull.encode("utf-8"))
                    self._node._write_receipt(
                        document_type="mail_receive_receipt",
                        payload={
                            "receiver": self._node.node_info.node_id,
                            "sender": peer_node_id,
                            "message_id": item_id,
                            "message_type": msg_type,
                            "receipt": {
                                "status": "stored",
                                "payload_bytes": _pb_pull,
                                "payload_hash": _ph_pull,
                            },
                        },
                        counterparty=peer_node_id,
                        order_ref=item_id,
                        proof_purpose="acknowledgment",
                        sign=True,
                    )
                elif not self._receipt_skip_warned:
                    self._log.warning("RECEIPT_SKIP: node missing _write_receipt — mail receipts disabled")
                    self._receipt_skip_warned = True
                if is_system:
                    item["_sender_pubkey"] = getattr(resp, "public_key", "")
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


MailSyncEngine = SyncEngine
