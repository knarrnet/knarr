"""knarr-mail skill handler: agent-to-agent messaging.

Actions:
    send  — deliver a message to a local or remote recipient
    poll  — retrieve messages (local only)
    ack   — mark messages read/archived/deleted (local only)
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Module-level node reference, injected via set_node()
_node = None
_mail_config: Dict[str, Any] = {}
_debug = False
_group_engine = None

MAX_TTL_HOURS = 168  # 7 days
DEFAULT_TTL_HOURS = 72
MAX_POLL_LIMIT = 200
DEFAULT_POLL_LIMIT = 50
MAX_MESSAGES = 10000
VALID_DISPOSITIONS = {"read", "archived", "deleted"}
MAX_ACK_BATCH = 100
MAX_ATTACHMENTS = 10
MAX_INLINE_SIZE = 10 * 1024 * 1024  # 10 MB per inline attachment
SPILLOVER_THRESHOLD = 60 * 1024  # 60KB


def set_node(node):
    """Called by the node framework to inject the DHTNode reference."""
    global _node, _mail_config, _debug, _group_engine, _x25519_private
    _node = node
    _mail_config = node._config.get("mail", {})
    _debug = _mail_config.get("debug", False)
    _group_engine = getattr(node, '_group_engine', None)
    _x25519_private = getattr(node, '_x25519_private', None)

def _decrypt_mail_item(input_data: dict) -> bool:
    """Returns True if successful (or not encrypted), False if decryption failed."""
    if "encrypted_body" not in input_data:
        return True
        
    if not _x25519_private:
        logger.warning("Cannot decrypt mail: node missing X25519 private key")
        return False
        
    try:
        from nacl.public import SealedBox
        box = SealedBox(_x25519_private)
        raw = base64.b64decode(input_data["encrypted_body"])
        decrypted = box.decrypt(raw)
        
        input_data["body"] = json.loads(decrypted.decode('utf-8'))
        input_data["_encrypted"] = True
        return True
    except Exception as e:
        logger.warning(f"Failed to decrypt mail item: {e}")
        return False


async def handle(input_data: dict) -> dict:
    """Main handler dispatching to send/poll/ack actions.

    TP-1: Made async — _handle_send, _handle_poll, _handle_get_message are async
    due to asyncio.to_thread calls in spillover paths.
    """
    if _node is None:
        return {"error": "mail_not_initialized", "message": "Mail handler not initialized"}

    action = input_data.get("action", "")
    if action == "send":
        return await _handle_send(input_data)
    elif action == "poll":
        return await _handle_poll(input_data)
    elif action == "ack":
        return _handle_ack(input_data)
    elif action == "get_message":
        return await _handle_get_message(input_data)
    elif action == "poll_results":
        return _handle_poll_results(input_data)
    elif action == "check_upgrade":
        # v0.32.0: P1 — on-demand upgrade trigger (local-only)
        return _handle_check_upgrade(input_data)
    else:
        return {"error": "unknown_action", "message": f"Unknown action: {action}"}


def _get_caller_node_id(input_data: dict) -> str:
    """Extract authenticated caller identity from input_data."""
    return input_data.get("_caller_node_id", "")


def _is_local_call(input_data: dict) -> bool:
    """Check if the caller is this node itself."""
    caller = _get_caller_node_id(input_data)
    return caller == _node.node_info.node_id


def _is_valid_hash(h: str) -> bool:
    """Validate a 64-char lowercase hex hash."""
    return isinstance(h, str) and len(h) == 64 and all(c in '0123456789abcdef' for c in h)


def _process_attachments(body: dict) -> Optional[dict]:
    """Process inline attachments: store data on sidecar, replace with URIs.

    Returns error dict on failure, None on success (body mutated in-place).
    """
    attachments = body.get("attachments")
    if not attachments:
        return None
    if not isinstance(attachments, list):
        return {"error": "invalid_attachments", "message": "attachments must be a list"}
    if len(attachments) > MAX_ATTACHMENTS:
        return {"error": "too_many_attachments", "message": f"max {MAX_ATTACHMENTS} attachments"}

    resolved = []
    for i, att in enumerate(attachments):
        if isinstance(att, str):
            # URI reference: "knarr-asset://<hash>" or bare hash
            if att.startswith("knarr-asset://"):
                h = att[len("knarr-asset://"):]
            else:
                h = att
            if not _is_valid_hash(h):
                return {"error": "invalid_attachment", "message": f"attachment {i}: invalid hash"}
            resolved.append({"uri": f"knarr-asset://{h}", "name": ""})

        elif isinstance(att, dict):
            if "data" in att:
                # Inline base64: decode and store on local sidecar
                if not hasattr(_node, 'store_asset') or not _node._asset_dir:
                    return {"error": "sidecar_unavailable",
                            "message": "recipient has no sidecar for inline attachments"}
                try:
                    raw = base64.b64decode(att["data"], validate=True)
                except Exception:
                    return {"error": "invalid_attachment",
                            "message": f"attachment {i}: invalid base64 data"}
                if len(raw) > MAX_INLINE_SIZE:
                    return {"error": "attachment_too_large",
                            "message": f"attachment {i}: exceeds {MAX_INLINE_SIZE} bytes"}
                content_hash = _node.store_asset(raw)
                resolved.append({
                    "uri": f"knarr-asset://{content_hash}",
                    "name": att.get("name", ""),
                    "size": len(raw),
                })
            elif "uri" in att:
                # Already-resolved URI reference
                uri = att["uri"]
                h = uri[len("knarr-asset://"):] if uri.startswith("knarr-asset://") else uri
                if not _is_valid_hash(h):
                    return {"error": "invalid_attachment", "message": f"attachment {i}: invalid hash"}
                resolved.append({"uri": f"knarr-asset://{h}", "name": att.get("name", "")})
            else:
                return {"error": "invalid_attachment",
                        "message": f"attachment {i}: must have 'data' or 'uri'"}
        else:
            return {"error": "invalid_attachment",
                    "message": f"attachment {i}: must be string or dict"}

    body["attachments"] = resolved
    return None


async def _reassemble_spillover(msg_dict: dict) -> dict:
    """If message has spillover, fetch from sender's sidecar and reassemble.

    TP-1: Made async — contains await asyncio.to_thread for network fetch.
    TP-7: Uses storage.get_address() instead of raw _get_conn().
    """
    import hashlib
    metadata = msg_dict.get("metadata") or {}
    spillover_hash = metadata.get("_spillover_hash")
    if not spillover_hash:
        return msg_dict

    # H-4: Validate hash format before any URL construction
    if not _is_valid_hash(spillover_hash):
        logger.warning(f"MAIL_SPILLOVER_REJECT invalid_hash={spillover_hash!r}")
        return msg_dict

    try:
        sender_id = msg_dict.get("from_node", "")
        if sender_id and _node:
            # TP-7: Look up sender via Storage API instead of raw _get_conn()
            addr = _node.storage.get_address(sender_id)
            if addr and addr.get("sidecar_port"):
                host = addr["last_ip"]
                sidecar_port = int(addr["sidecar_port"])

                # H-7: SSRF restriction — reject private/loopback/link-local hosts
                import ipaddress
                try:
                    addr = ipaddress.ip_address(host)
                    if addr.is_private or addr.is_loopback or addr.is_link_local:
                        logger.warning(f"MAIL_SPILLOVER_REJECT host={host} reason=private_address")
                        raise ValueError("private address")
                except ValueError:
                    # hostname, not IP — allow (DNS resolution happens at fetch time)
                    if host in ("localhost", "127.0.0.1", "::1"):
                        logger.warning(f"MAIL_SPILLOVER_REJECT host={host} reason=loopback")
                        raise ValueError("loopback")

                # Apply peer override if configured
                resolved_host, _ = _node.resolve_peer(sender_id, host, 0)

                fetch_url = f"https://{resolved_host}:{sidecar_port}/assets/{spillover_hash}"
                import urllib.request, ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # sidecar uses self-signed certs
                req = urllib.request.Request(fetch_url)
                # C-04: urlopen blocks; run in thread to avoid stalling the event loop
                def _do_fetch():
                    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                        return resp.read()
                raw_bytes = await asyncio.to_thread(_do_fetch)

                # M-10: Verify content hash matches requested hash
                content_hash = hashlib.sha256(raw_bytes).hexdigest()
                if content_hash != spillover_hash:
                    logger.warning(f"MAIL_SPILLOVER_HASH_MISMATCH expected={spillover_hash[:16]} got={content_hash[:16]}")
                else:
                    full_body = raw_bytes.decode("utf-8")
                    # H-3: Parse JSON if body was originally a dict
                    try:
                        msg_dict["body"] = json.loads(full_body)
                    except (json.JSONDecodeError, ValueError):
                        msg_dict["body"] = full_body
                    return msg_dict

        # Sender offline or sidecar not found — return truncated with note
        body = msg_dict.get("body", "")
        if "[Message truncated" not in str(body):
            msg_dict["body"] = str(body) + "\n\n[Spillover content unavailable — sender may be offline]"
    except Exception as e:
        logger.warning(f"MAIL_SPILLOVER_FETCH_FAIL hash={spillover_hash[:16]}: {e}")

    return msg_dict


async def _handle_send(input_data: dict) -> dict:
    """Handle 'send' action — store a message from a remote caller.

    TP-1: Made async — contains await asyncio.to_thread for spillover upload.
    """
    if not _decrypt_mail_item(input_data):
        return {"status": "rejected", "reason": "decryption_failed"}

    caller_node_id = _get_caller_node_id(input_data)

    # accept_from is an INBOUND policy — "who can send mail TO me."
    # Local calls (node's own thrall/skill sending outbound mail) bypass it.
    if not _is_local_call(input_data):
        accept_from = _mail_config.get("accept_from", "all")
        if accept_from == "none":
            return {"status": "rejected", "reason": "mailbox_disabled"}

        if accept_from == "whitelist":
            whitelist = _mail_config.get("whitelist", [])
            if caller_node_id not in whitelist:
                return {"status": "rejected", "reason": "not_whitelisted"}
        elif accept_from == "groups":
            accept_groups = set(_mail_config.get("accept_groups", []))
            if not accept_groups:
                # Misconfiguration: accept_from=groups but no accept_groups defined — fail-closed
                return {"status": "rejected", "reason": "no_accept_groups_configured"}
            if _group_engine:
            caller_groups = set(_group_engine.get_groups(caller_node_id))
            if not caller_groups.intersection(accept_groups):
                return {"status": "rejected", "reason": "not_in_accepted_groups"}
        else:
            # Group engine not available — reject to be safe (fail-closed)
            return {"status": "rejected", "reason": "groups_not_available"}

    # Validate body
    body = input_data.get("body")
    if not isinstance(body, dict):
        return {"error": "invalid_body", "message": "body must be a dict"}

    # Check inbox capacity BEFORE storing attachments (V010-003)
    # v0.29.1: Only inbox triggers rejection — system/jobreport mail flows regardless
    max_inbox = int(_mail_config.get("max_inbox", _mail_config.get("max_messages", MAX_MESSAGES)))
    count = _node.storage.count_mail_inbox()
    if count >= max_inbox:
        logger.warning(f"INBOX_FULL count={count} max={max_inbox} from={caller_node_id[:16]}")
        return {"status": "rejected", "reason": "mailbox_full"}

    if isinstance(body, dict):
        body.pop("_encrypted", None)  # Strip caller-controlled flag; set only by decryption path
        # process attachments
        body_copy = dict(body)
        if "attachments" not in body_copy and "attachments" in input_data:
            body_copy["attachments"] = input_data["attachments"]
        att_err = _process_attachments(body_copy)
        if att_err:
            return att_err
        body = body_copy
    else:
        # If it's not a dict, we can't inject _encrypted directly. Re-wrap or skip?
        # A valid body must be a dict
        return {"error": "invalid_body", "message": "body must be a dict"}


    msg_type = body.get("type", "text")
    if isinstance(msg_type, str) and msg_type:
        msg_type = msg_type.lower()
    else:
        msg_type = "text"

    session_id = body.get("session_id", None)
    reply_to = body.get("reply_to", None)

    # TTL
    ttl_hours = input_data.get("ttl_hours", DEFAULT_TTL_HOURS)
    try:
        ttl_hours = float(ttl_hours)
    except (TypeError, ValueError):
        ttl_hours = DEFAULT_TTL_HOURS
    if ttl_hours <= 0 or ttl_hours > MAX_TTL_HOURS:
        ttl_hours = min(max(ttl_hours, 1), MAX_TTL_HOURS)

    # Determine recipient: explicit "to" field, or default to handler's own node
    to_node = input_data.get("to") or body.get("to") or _node.node_info.node_id

    # Generate server-assigned ID and timestamp
    message_id = str(uuid.uuid4())
    now = time.time()
    ttl_expires = now + (ttl_hours * 3600)

    # Check body size, spill overflow to sidecar if needed
    body_str = json.dumps(body) if isinstance(body, dict) else str(body)
    metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
    if len(body_str.encode("utf-8")) > SPILLOVER_THRESHOLD:
        try:
            # Upload overflow to sender's sidecar
            spillover_key = f"spillover_{message_id}.md"
            sidecar_port = getattr(_node, '_sidecar_port', 0) or 0
            if sidecar_port:
                import urllib.request, ssl, hashlib as _hl
                upload_data = body_str.encode("utf-8")
                content_hash = _hl.sha256(upload_data).hexdigest()
                timestamp = str(int(time.time()))
                # Sidecar Ed25519 auth: sign "{method}:{path}:{timestamp}:{content_hash}"
                auth_payload = f"PUT:/assets:{timestamp}:{content_hash}".encode("utf-8")
                signing_key = getattr(_node, '_signing_key', None)
                pub_key_hex = getattr(_node, '_public_key_hex', '')
                auth_headers = {}
                if signing_key and pub_key_hex:
                    signed = signing_key.sign(auth_payload)
                    auth_headers = {
                        "X-Knarr-PublicKey": pub_key_hex,
                        "X-Knarr-Signature": signed.signature.hex(),
                        "X-Knarr-Timestamp": timestamp,
                        "X-Knarr-Content-Hash": content_hash,
                    }
                upload_url = f"https://127.0.0.1:{sidecar_port}/assets"
                req = urllib.request.Request(
                    upload_url, method="PUT",
                    data=upload_data,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(upload_data)),
                        **auth_headers,
                    }
                )
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # sidecar uses self-signed certs
                # C-04: urlopen blocks; run in thread to avoid stalling the event loop
                _req_ref = req
                _ctx_ref = ctx
                def _do_upload():
                    with urllib.request.urlopen(_req_ref, timeout=10, context=_ctx_ref) as resp:
                        return json.loads(resp.read())
                resp_data = await asyncio.to_thread(_do_upload)
                spillover_hash = resp_data.get("hash", "")

                # Truncate body, add spillover reference
                if spillover_hash:
                    if isinstance(body, dict):
                        # Safely truncate just the content if possible, or build a new dict
                        if "content" in body and isinstance(body["content"], str):
                            body["content"] = body["content"][:30000] + "\n\n[Message truncated. Full content available via spillover.]"
                        else:
                            body = {"content": "[Message truncated...]", "metadata": metadata}
                        metadata["_spillover_hash"] = spillover_hash
                        body["metadata"] = metadata
                    else:
                        pass_str = body_str[:SPILLOVER_THRESHOLD].rstrip()
                        pass_str += f"\n\n[Message truncated. Full content available via spillover.]"
                        body = pass_str
        except Exception as e:
            logger.warning(f"MAIL_SPILLOVER_UPLOAD_FAIL msg={message_id}: {e}")
            # Fall through — send full message, may be truncated by protocol cap

    body_json = json.dumps(body)

    # V17-013: Route cross-node mail through SyncEngine for store-and-forward
    if to_node != _node.node_info.node_id:
        try:
            loop = _node._main_loop
            future = asyncio.run_coroutine_threadsafe(
                _node._sync.enqueue(
                    to_node=to_node,
                    msg_type=msg_type,
                    body=body,
                    session_id=session_id,
                    reply_to=reply_to,
                    ttl_hours=ttl_hours,
                ),
                loop,
            )
            future.result(timeout=5)
        except Exception as e:
            logger.error(f"Failed to enqueue mail to {to_node[:16]}: {e}")
            return {"error": "enqueue_failed", "message": str(e)}

        if _debug:
            logger.info(f"HANDLER_SEND_QUEUE to={to_node[:16]} from={caller_node_id[:16]} type={msg_type} id={message_id[:8]}")

        return {
            "status": "queued",
            "message_id": message_id,
            "timestamp": now,
            "to_node": to_node,
        }

    # Local delivery — store directly, routed to correct bucket
    _node.storage.store_mail(
        message_id=message_id,
        from_node=caller_node_id,
        to_node=_node.node_info.node_id,
        timestamp=now,
        body=body_json,
        session_id=session_id,
        msg_type=msg_type,
        reply_to=reply_to,
        ttl_expires=ttl_expires,
        system=False,  # handler sends are user-initiated
    )

    if _debug:
        logger.info(f"HANDLER_SEND_LOCAL from={caller_node_id[:16]} type={msg_type} id={message_id[:8]}")

    return {
        "status": "delivered",
        "message_id": message_id,
        "timestamp": now,
    }


def _resolve_poll_attachments(body: dict):
    """Enrich attachments with local availability info (mutates body)."""
    atts = body.get("attachments")
    if not isinstance(atts, list):
        return
    asset_dir = getattr(_node, '_asset_dir', '')
    for att in atts:
        if not isinstance(att, dict) or "uri" not in att:
            continue
        uri = att["uri"]
        h = uri[len("knarr-asset://"):] if uri.startswith("knarr-asset://") else ""
        if not _is_valid_hash(h):
            att["available"] = False
            continue
        if asset_dir and os.path.isfile(os.path.join(asset_dir, h)):
            att["available"] = True
            try:
                att["size"] = os.path.getsize(os.path.join(asset_dir, h))
            except OSError:
                pass
        else:
            att["available"] = False


async def _handle_poll(input_data: dict) -> dict:
    """Handle 'poll' action — retrieve messages. Local-only.

    TP-1: Made async — calls async _reassemble_spillover.
    """
    if not _is_local_call(input_data):
        return {"error": "local_only", "message": "poll and ack are local-only actions"}

    since = input_data.get("since")
    since_rowid = 0
    if since is not None:
        try:
            since_rowid = int(since)
        except (TypeError, ValueError):
            since_rowid = 0

    filters = input_data.get("filters", {})
    if not isinstance(filters, dict):
        filters = {}

    limit = input_data.get("limit", DEFAULT_POLL_LIMIT)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_POLL_LIMIT
    limit = min(max(1, limit), MAX_POLL_LIMIT)

    from_node = filters.get("from")
    session_id = filters.get("session_id")
    msg_type = filters.get("type")
    status_filter = filters.get("status", "unread")
    system_filter = filters.get("system")

    rows, gap = _node.storage.poll_mail(
        to_node=_node.node_info.node_id,
        since_rowid=since_rowid,
        from_node=from_node,
        session_id=session_id,
        msg_type=msg_type,
        status=status_filter,
        limit=limit,
        system=system_filter,
    )

    messages = []
    last_rowid = 0
    for row in rows:
        try:
            body_parsed = json.loads(row["body"])
        except (json.JSONDecodeError, TypeError):
            body_parsed = row["body"]
        # Resolve attachment availability
        if isinstance(body_parsed, dict) and "attachments" in body_parsed:
            _resolve_poll_attachments(body_parsed)
        
        msg_out = {
            "message_id": row["message_id"],
            "from_node": row["from_node"],
            "timestamp": row["timestamp"],
            "body": body_parsed,
            "msg_type": row.get("msg_type", "text"),
            "session_id": row.get("session_id"),
            "status": row["status"],
            "encrypted": body_parsed.get("_encrypted", False) if isinstance(body_parsed, dict) else False,
        }
        if isinstance(body_parsed, dict) and "metadata" in body_parsed:
            msg_out["metadata"] = body_parsed["metadata"]
            
        messages.append(await _reassemble_spillover(msg_out))
        last_rowid = row["rowid"]

    next_token = str(last_rowid) if last_rowid > 0 else None
    total_unread = _node.storage.count_mail_unread(_node.node_info.node_id, system=system_filter)

    if _debug:
        logger.info(f"HANDLER_POLL count={len(messages)} unread={total_unread} status={status_filter}")

    return {
        "messages": messages,
        "next_token": next_token,
        "gap": gap,
        "total_unread": total_unread,
    }


async def _handle_get_message(input_data: dict) -> dict:
    """Handle 'get_message' action — retrieve a single message by ID. Local-only.

    TP-1: Made async — calls async _reassemble_spillover.
    """
    if not _is_local_call(input_data):
        return {"error": "local_only", "message": "get_message is a local-only action"}

    message_id = input_data.get("message_id", "")
    if not message_id:
        return {"error": "invalid_input", "message": "message_id is required"}

    row = _node.storage.get_mail_message(message_id, _node.node_info.node_id)
    if not row:
        return {"error": "not_found", "message": f"Message {message_id} not found"}

    try:
        body_parsed = json.loads(row["body"])
    except (json.JSONDecodeError, TypeError):
        body_parsed = row["body"]

    if isinstance(body_parsed, dict) and "attachments" in body_parsed:
            _resolve_poll_attachments(body_parsed)

    msg_out = {
        "message_id": row["message_id"],
        "from_node": row["from_node"],
        "timestamp": row["timestamp"],
        "body": body_parsed,
        "msg_type": row.get("msg_type", "text"),
        "session_id": row.get("session_id"),
        "status": row["status"],
        "encrypted": body_parsed.get("_encrypted", False) if isinstance(body_parsed, dict) else False,
    }
    if isinstance(body_parsed, dict) and "metadata" in body_parsed:
        msg_out["metadata"] = body_parsed["metadata"]

    return {
        "message": await _reassemble_spillover(msg_out)
    }


def _handle_ack(input_data: dict) -> dict:
    """Handle 'ack' action — mark messages read/archived/deleted. Local-only."""
    if not _is_local_call(input_data):
        return {"error": "local_only", "message": "poll and ack are local-only actions"}

    message_ids = input_data.get("message_ids", [])
    if not isinstance(message_ids, list):
        return {"error": "invalid_input", "message": "message_ids must be a list"}
    if len(message_ids) > MAX_ACK_BATCH:
        return {"error": "invalid_input", "message": f"max {MAX_ACK_BATCH} ids per ack"}

    disposition = input_data.get("disposition", "read")
    if disposition not in VALID_DISPOSITIONS:
        return {"error": "invalid_disposition",
                "message": f"disposition must be one of: {', '.join(sorted(VALID_DISPOSITIONS))}"}

    count = _node.storage.ack_mail(
        message_ids=message_ids,
        to_node=_node.node_info.node_id,
        disposition=disposition,
    )

    return {"acknowledged": count}


def _handle_poll_results(input_data: dict) -> dict:
    """E4: Handle 'poll_results' action — retrieve completed/failed task results. Local-only."""
    if not _is_local_call(input_data):
        return {"error": "local_only", "message": "poll_results is a local-only action"}

    limit = input_data.get("limit", 20)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = min(max(1, limit), 50)

    status_filter = input_data.get("status", "unread")
    if status_filter not in ("unread", "read", "all"):
        status_filter = "unread"

    results = _node.storage.poll_task_results(limit, status_filter)
    return {"results": results, "count": len(results)}


def _handle_check_upgrade(input_data: dict) -> dict:
    """v0.32.0: P1 — Trigger immediate upgrade check. Local-only.

    Queries the upstream release API and returns current + available version.
    This is a synchronous check; it does NOT initiate an actual upgrade.
    """
    if not _is_local_call(input_data):
        return {"error": "local_only", "message": "check_upgrade is a local-only action"}
    try:
        from knarr.dht.upgrade import get_latest_version
        from knarr import __version__
        available = get_latest_version()
        result = {
            "current_version": __version__,
            "available_version": available,
            "upgrade_available": available is not None and available != __version__,
        }
        if _debug:
            logger.info(f"UPGRADE_CHECK current={__version__} available={available}")
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.warning(f"UPGRADE_CHECK_FAIL: {e}")
        return {"status": "error", "error": str(e)}
