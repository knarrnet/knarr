import asyncio
from collections import deque
import hmac
import json
import logging
import os
import sys
import time
import tomllib
import urllib.parse
from typing import Dict, Any, Optional
import ipaddress

def _check_ip_whitelist(client_ip: str, allowed_ips: list) -> bool:
    """Check if client IP is in the allowed list. Empty list = allow all."""
    if not allowed_ips:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
        for entry in allowed_ips:
            try:
                if "/" in entry:
                    if addr in ipaddress.ip_network(entry, strict=False):
                        return True
                else:
                    if addr == ipaddress.ip_address(entry):
                        return True
            except ValueError:
                continue
        return False
    except ValueError:
        return False

logger = logging.getLogger(__name__)

# Extended MIME types for static file serving (knarr-static)
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".pdf": "application/pdf",
}

def _html_escape(s: str) -> str:
    """Escape string for safe HTML embedding."""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;")


class RingBufferHandler(logging.Handler):
    """In-memory log sink for the cockpit logs endpoint."""

    def __init__(self, maxlen: int = 1000):
        super().__init__()
        self._records = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append({
            "created": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })

    def tail(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), len(self._records) or 1))
        return list(self._records)[-limit:]


class CockpitServer:
    """Lightweight HTTP server for the Knarr Cockpit dashboard."""

    def __init__(self, node, bind: str = "127.0.0.1", port: int = 8080, auth_token: str = "",
                 exposures: Optional[Dict[str, Any]] = None, config_dir: str = "",
                 cert_path: str = "", key_path: str = "", tls_mode: str = "auto"):
        self._node = node
        self._bind = bind
        self._port = port
        self._auth_token = auth_token
        self._config_dir = config_dir or os.getcwd()
        self._cert_path = cert_path
        self._key_path = key_path
        self._tls_mode = tls_mode if tls_mode in ("auto", "off", "both") else "auto"
        self._server = None
        self._server_plain = None  # HTTP server for "both" mode
        self._max_connections = 8
        self._active_connections = 0
        self._exposures = self._build_exposure_index(exposures or {})
        self._rate_limits: Dict[str, Dict[str, list]] = {}  # path -> {ip: [timestamps]}
        self._token_counters: Dict[str, Dict[str, dict]] = {}  # path -> {token: {count, day}}
        # v0.38.0 A3.2: Wallet HMAC auth + spending caps
        self._wallet_daily_spent: float = 0.0
        self._wallet_daily_reset: float = time.time()
        # FIX-002: HMAC replay guard — track seen signatures within timestamp window
        self._seen_wallet_sigs: dict = {}  # {sig_hex: expiry_time}
        self._sig_sweep_interval: float = 60.0
        self._last_sig_sweep: float = time.time()
        # FIX-003: Spend cap lock to prevent TOCTOU race
        import asyncio as _asyncio
        self._wallet_send_lock = _asyncio.Lock()
        self._routing_policy = self._load_routing_policy()
        self._log_handler = RingBufferHandler(maxlen=1000)
        logging.getLogger().addHandler(self._log_handler)

    @property
    def port(self) -> int:
        return self._port

    async def start(self):
        """Start the Cockpit server."""
        if self._bind != "127.0.0.1" and not self._auth_token:
            logger.warning("Cockpit bound to %s without auth_token — admin endpoints are unprotected!", self._bind)

        ssl_ctx = None
        if self._tls_mode != "off" and self._cert_path and self._key_path:
            import ssl as _ssl
            ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
            ssl_ctx.load_cert_chain(self._cert_path, self._key_path)

        if self._tls_mode == "both" and ssl_ctx:
            # HTTP on self._port, HTTPS on self._port + 1
            self._server_plain = await asyncio.start_server(
                self._handle_connection, self._bind, self._port
            )
            sock_plain = self._server_plain.sockets[0]
            self._port = sock_plain.getsockname()[1]
            https_port = self._port + 1
            self._server = await asyncio.start_server(
                self._handle_connection, self._bind, https_port, ssl=ssl_ctx
            )
            self._https_port = self._server.sockets[0].getsockname()[1]
            logger.info(f"Cockpit dashboard listening on {self._bind}:{self._port} (HTTP) + :{self._https_port} (HTTPS)")
        else:
            # Single server: HTTPS (auto) or HTTP (off)
            use_ssl = ssl_ctx if self._tls_mode != "off" else None
            self._server = await asyncio.start_server(
                self._handle_connection, self._bind, self._port, ssl=use_ssl
            )
            sock = self._server.sockets[0]
            self._port = sock.getsockname()[1]
            tls_label = "HTTPS" if use_ssl else "HTTP"
            logger.info(f"Cockpit dashboard listening on {self._bind}:{self._port} ({tls_label})")

    async def stop(self):
        """Stop the Cockpit server."""
        if self._server_plain:
            self._server_plain.close()
            await self._server_plain.wait_closed()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Cockpit dashboard stopped")
        logging.getLogger().removeHandler(self._log_handler)

    @staticmethod
    def _build_exposure_index(exposures: Dict[str, Any]) -> Dict[str, dict]:
        """Build path -> exposure config lookup from [expose.*] TOML sections."""
        index = {}
        for name, cfg in exposures.items():
            if not isinstance(cfg, dict):
                continue
            path = cfg.get("path", name)
            if not cfg.get("enabled", True):
                continue
            index[path] = {
                "name": name,
                "skill": cfg.get("skill", name),
                "path": path,
                "presets": cfg.get("presets", {}),
                "fields": cfg.get("fields", {}),
                "display": cfg.get("display", {}),
                "provider": cfg.get("provider", {}),
                "rate_limit": int(cfg.get("rate_limit", 10)),
                "auth": cfg.get("auth", "none"),
                "tokens": cfg.get("tokens", []),
                "max_calls_per_token": int(cfg.get("max_calls_per_token", 0)),
                "max_calls_per_day": int(cfg.get("max_calls_per_day", 0)),
                "mode": cfg.get("mode", "auto"),
                "timeout": int(cfg.get("timeout", 30)),
                "timeout_ms": int(cfg.get("timeout_ms", 0)),
                "payment": cfg.get("payment", "none"),
                "payment_asset": cfg.get("payment_asset", ""),
                "payment_assets": cfg.get("payment_assets", []),
                "payment_address": cfg.get("payment_address", ""),
                "payment_network": cfg.get("payment_network", ""),
                "payment_amount": cfg.get("payment_amount", ""),
            }
        return index

    def _load_routing_policy(self) -> Dict[str, Any]:
        """Load routing.toml from the cockpit config dir with safe defaults."""
        policy: Dict[str, Any] = {"defaults": {"local_weight": 1.0}}
        routing_path = os.path.join(self._config_dir, "routing.toml")
        if not os.path.exists(routing_path):
            return policy
        try:
            with open(routing_path, "rb") as handle:
                raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            logger.warning("ROUTING_CONFIG_INVALID path=%s error=%s", routing_path, exc)
            return policy
        except Exception as exc:
            logger.warning("ROUTING_CONFIG_LOAD_FAIL path=%s error=%s", routing_path, exc)
            return policy

        if isinstance(raw, dict):
            policy.update(raw)
        defaults = raw.get("defaults", {}) if isinstance(raw, dict) else {}
        local_weight = defaults.get("local_weight", 1.0) if isinstance(defaults, dict) else 1.0
        try:
            local_weight = float(local_weight)
        except (TypeError, ValueError):
            logger.warning("ROUTING_LOCAL_WEIGHT_INVALID path=%s value=%r", routing_path, local_weight)
            local_weight = 1.0
        clamped_weight = max(0.0, min(2.0, local_weight))
        if clamped_weight != local_weight:
            logger.warning(
                "ROUTING_LOCAL_WEIGHT_CLAMPED path=%s value=%s clamped=%s",
                routing_path, local_weight, clamped_weight,
            )
        policy["defaults"] = {"local_weight": clamped_weight}
        return policy

    def get_local_weight(self, skill_name=None) -> float:
        """Return the configured local routing weight for /api/execute scoring."""
        return float(self._routing_policy.get("defaults", {}).get("local_weight", 1.0))

    def _select_execute_provider(self, skill: str) -> Optional[Dict[str, Any]]:
        candidates = []
        all_skills = self._node.get_skills()
        for skill_info in all_skills.get("network", []):
            if skill_info["name"].lower() == skill.lower():
                candidates.extend(skill_info.get("providers", []))
                break

        if skill.lower() in getattr(self._node, "_handlers", {}):
            candidates.append({
                "node_id": self._node.node_info.node_id,
                "host": "127.0.0.1",
                "port": self._node.node_info.port,
                "_local": True,
            })

        if not candidates:
            return None

        local_weight = self.get_local_weight(skill)
        scored = []
        for index, candidate in enumerate(candidates):
            normalized = dict(candidate)
            score = 1.0
            if normalized.get("_local"):
                score *= local_weight
            scored.append((score, 0 if normalized.get("_local") else 1, index, normalized))
        scored.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
        return scored[0][3]

    _MAX_RATE_LIMIT_IPS = 4096  # per path

    def _check_rate_limit(self, path: str, ip: str) -> bool:
        """Returns True if the request is allowed, False if rate-limited."""
        exposure = self._exposures.get(path)
        if not exposure:
            return True
        limit = exposure["rate_limit"]
        now = time.monotonic()
        window = 60.0  # 1 minute
        if path not in self._rate_limits:
            self._rate_limits[path] = {}
        ip_map = self._rate_limits[path]
        timestamps = ip_map.get(ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < window]
        if len(timestamps) >= limit:
            ip_map[ip] = timestamps
            return False
        timestamps.append(now)
        ip_map[ip] = timestamps
        # Evict oldest IPs if map too large
        if len(ip_map) > self._MAX_RATE_LIMIT_IPS:
            oldest_ip = min(ip_map, key=lambda k: ip_map[k][-1] if ip_map[k] else 0)
            del ip_map[oldest_ip]
        return True

    _MAX_RATE_LIMIT_TOKENS = 4096  # per exposure path

    def _check_exposure_auth(self, exposure: dict, headers: dict) -> Optional[str]:
        """Check exposure-level token auth. Returns token string if valid, None if rejected."""
        auth_mode = exposure.get("auth", "none")
        if auth_mode != "token":
            return ""  # no auth required, return empty token
        tokens = exposure.get("tokens", [])
        if not tokens:
            return None  # auth=token but no tokens configured — fail closed
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        provided = auth_header[7:]
        for t in tokens:
            if hmac.compare_digest(provided.encode(), str(t).encode()):
                return provided
        return None

    def _check_token_rate_limit(self, path: str, token: str, exposure: dict) -> bool:
        """Per-token and per-day rate limits. Returns True if allowed."""
        max_per_token = exposure.get("max_calls_per_token", 0)
        max_per_day = exposure.get("max_calls_per_day", 0)
        if max_per_token <= 0 and max_per_day <= 0:
            return True
        import datetime
        today = datetime.date.today().isoformat()
        if path not in self._token_counters:
            self._token_counters[path] = {}
        counters = self._token_counters[path]
        # Per-day (aggregate across all tokens)
        day_key = "__day__"
        day_entry = counters.get(day_key, {"count": 0, "day": today})
        if day_entry["day"] != today:
            day_entry = {"count": 0, "day": today}
        if max_per_day > 0 and day_entry["count"] >= max_per_day:
            return False
        # Per-token
        if token and max_per_token > 0:
            tok_entry = counters.get(token, {"count": 0, "day": today})
            if tok_entry["day"] != today:
                tok_entry = {"count": 0, "day": today}
            if tok_entry["count"] >= max_per_token:
                return False
            tok_entry["count"] += 1
            counters[token] = tok_entry
        day_entry["count"] += 1
        counters[day_key] = day_entry
        return True

    def _resolve_exposure(self, path: str) -> Optional[dict]:
        """Find exposure config for a /s/ path. Returns None if not found."""
        # path comes in as /s/xxx or /s/xxx/execute etc
        # Strip /s/ prefix and any trailing action
        return self._exposures.get(path)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle one HTTP request, then close."""
        if self._active_connections >= self._max_connections:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        self._active_connections += 1
        try:
            # Read request line
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return

            parts = request_line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) < 2:
                self._respond_error(writer, 400, "Bad Request")
                return

            method = parts[0].upper()
            url_parts = urllib.parse.urlparse(parts[1])
            path = url_parts.path
            query = urllib.parse.parse_qs(url_parts.query)

            # Read headers (max 64 to prevent abuse)
            headers = {}
            for _ in range(64):
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ": " in decoded:
                    key, value = decoded.split(": ", 1)
                    headers[key.lower()] = value

            # Parse body if present
            body = b""
            try:
                content_length = int(headers.get("content-length", "0"))
            except ValueError:
                self._respond_error(writer, 400, "Invalid Content-Length")
                return
            if content_length > 0:
                if content_length > 104857600:  # 100MB max (for uploads)
                    self._respond_error(writer, 413, "Request Too Large")
                    return
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=30.0)

            # Extract client IP for rate limiting and whitelisting
            client_ip = ""
            try:
                peer = writer.get_extra_info("peername")
                if peer:
                    client_ip = peer[0]
            except Exception:
                pass

            # IP whitelist check (before auth)
            allowed_ips = self._node._config.get("cockpit", {}).get("allowed_ips", [])
            if allowed_ips and not _check_ip_whitelist(client_ip, allowed_ips):
                self._respond_error(writer, 403, "Forbidden")
                return

            # Auth check for API endpoints
            # Exempt: GET /api/assets/* without remote proxy (local read-only)
            auth_exempt = False
            if auth_exempt is False and method == "GET" and path.startswith("/api/assets/") and not query.get("host"):
                auth_exempt = True
            if path.startswith("/api/") and not auth_exempt:
                if not self._check_auth(headers, source_ip=client_ip, endpoint=path):
                    self._respond_401(writer)
                    return

            # Routing — wrapped in dispatch timeout (5s) to prevent cockpit starvation
            async def _dispatch_request():
                if path.startswith("/s/"):
                    await self._route_exposure(method, path, body, query, writer, client_ip, headers)
                elif method == "GET":
                    if path == "/meta":
                        # Self-description: list available realms
                        realms = {}
                        for name, cfg in self._node._meta_realms.items():
                            realms[name] = {"queries": cfg.queries, "access": cfg.access}
                        self._respond_json(writer, {"realms": realms})
                    elif path.startswith("/meta/"):
                        # Serve cached meta file
                        parts = path[len("/meta/"):].split("/", 1)
                        if len(parts) != 2:
                            self._respond_error(writer, 400, "Expected /meta/{realm}/{query}")
                            return
                        realm, meta_query = parts[0], parts[1]

                        # Path confinement
                        if ".." in realm or ".." in meta_query or "/" in realm or "/" in meta_query or "\\" in realm or "\\" in meta_query:
                            self._respond_error(writer, 400, "Invalid path")
                            return

                        # Check realm exists
                        realm_cfg = self._node._meta_realms.get(realm)
                        if not realm_cfg:
                            self._respond_error(writer, 404, f"Unknown realm: {realm}")
                            return

                        # Access control (public endpoints skip cockpit HTTP auth)
                        access_level = realm_cfg.access.get(meta_query, "public")
                        if access_level == "authenticated":
                            if not self._check_auth(headers, source_ip=client_ip, endpoint=path):
                                self._respond_401(writer)
                                return
                        elif access_level == "caller_only":
                            # Caller-only realms need thin computation — defer to v0.29.0
                            self._respond_error(writer, 501, "caller_only not implemented yet")
                            return

                        # Serve cached file
                        from pathlib import Path
                        cache_file = Path(self._node._config.get("_config_dir", ".")) / "cache" / realm / f"{meta_query}.json"
                        if not cache_file.exists():
                            self._respond_error(writer, 404, f"No cached data for {realm}/{meta_query}")
                            return

                        try:
                            # Read raw string
                            data_str = cache_file.read_text(encoding="utf-8")
                            self._respond_cors(writer, "200 OK", "application/json", data_str.encode("utf-8"))
                        except Exception as e:
                            self._respond_error(writer, 500, str(e))
                            
                    elif path == "/api/status":
                        self._respond_json(writer, self._node.get_status())
                    elif path == "/api/messages":
                        try:
                            q_status = query.get("status", ["unread"])[0]
                            q_limit = min(int(query.get("limit", ["200"])[0]), 500)
                            q_since = int(query.get("since", ["0"])[0])
                            q_system = query.get("system", ["0"])[0]
                            sys_filter = None if q_system == "all" else int(q_system)
                            filters = {"status": q_status}
                            if sys_filter is not None:
                                filters["system"] = sys_filter
                            res = await self._node.call_local("knarr-mail", {"action": "poll", "since": q_since, "filters": filters, "limit": q_limit})
                            self._respond_json(writer, res)
                        except Exception as e:
                            self._respond_json(writer, {"messages": [], "error": str(e)})
                    elif path.startswith("/api/messages/") and method == "GET":
                        msg_id = path[len("/api/messages/"):]
                        try:
                            res = await self._node.call_local("knarr-mail", {"action": "get_message", "message_id": msg_id})
                            self._respond_json(writer, res)
                        except Exception as e:
                            self._respond_error(writer, 404, str(e))
                    elif path == "/api/address_book":
                        try:
                            explicit = self._node.storage.get_addresses_by_tier("explicit")
                            cached = self._node.storage.get_addresses_by_tier("cached")
                            remote = self._node.storage.get_addresses_by_tier("remote")
                            self._respond_json(writer, {"explicit": explicit, "cached": cached, "remote": remote})
                        except Exception as e:
                            self._respond_error(writer, 500, str(e))
                    elif path == "/api/peers":
                        self._respond_json(writer, self._node.get_peers())
                    elif path == "/api/skills":
                        self._respond_json(writer, self._node.get_skills())
                    elif path == "/api/tasks":
                        self._respond_json(writer, self._node.get_tasks())
                    elif path == "/api/ledger":
                        self._respond_json(writer, self._node.get_ledger())
                    elif path.startswith("/api/jobs/") and path.endswith("/result"):
                        job_id = path[len("/api/jobs/"):-len("/result")]
                        await self._handle_job_result(writer, job_id)
                    elif path.startswith("/api/jobs/"):
                        job_id = path[len("/api/jobs/"):]
                        await self._handle_job_status(writer, job_id)
                    elif path == "/api/economy":
                        self._respond_json(writer, self._node.get_economy_summary())
                    elif path == "/api/results":
                        # E4: Unified task result retrieval
                        limit = min(int(query.get("limit", ["20"])[0]), 50)
                        status_filter = query.get("status", ["unread"])[0]
                        results = self._node.storage.poll_task_results(limit, status_filter)
                        self._respond_json(writer, {"results": results, "count": len(results)})
                    elif path == "/api/logs":
                        self._handle_logs(writer, query)
                    elif path == "/api/reputation":
                        self._respond_json(writer, self._node.get_reputation_summary())
                    elif path == "/api/secrets":
                        self._respond_json(writer, self._node.get_secrets_summary())
                    elif path.startswith("/api/secrets/"):
                        skill_name = path[len("/api/secrets/"):]
                        summary = self._node.get_secrets_summary()
                        if skill_name in summary:
                            self._respond_json(writer, summary[skill_name])
                        else:
                            self._respond_json(writer, {})
                    elif path == "/api/groups":
                        self._handle_groups_list(writer)
                    elif path.startswith("/api/groups/") and path.endswith("/members"):
                        group_name = path[len("/api/groups/"):-len("/members")]
                        self._handle_group_members(writer, group_name)
                    elif path == "/api/pricing/discounts":
                        self._handle_pricing_discounts_list(writer)
                    elif path == "/api/exposures":
                        self._handle_exposure_list(writer)
                    elif path == "/api/assets":
                        self._handle_asset_list(writer)
                    elif path.startswith("/api/assets/"):
                        asset_hash = path[len("/api/assets/"):]
                        await self._handle_asset_download(writer, asset_hash, query)
                    elif path.startswith("/api/receipts/"):
                        # v0.32.0: GET /api/receipts/{reference} — fetch credit note by job_id
                        reference = path[len("/api/receipts/"):]
                        await self._handle_receipt_fetch(writer, reference)
                    else:
                        try:
                            content, content_type = self._serve_static(path)
                            self._respond(writer, "200 OK", content_type, content)
                        except FileNotFoundError:
                            self._respond_404(writer)

                elif method == "POST":
                    if path == "/api/execute":
                        await self._handle_api_execute(writer, body)
                    elif path == "/api/messages/ack":
                        try:
                            data = json.loads(body)
                            res = await self._node.call_local("knarr-mail", {"action": "ack", "message_ids": data.get("message_ids", [])})
                            self._respond_json(writer, res)
                        except Exception as e:
                            self._respond_error(writer, 400, str(e))
                    elif path == "/api/messages/send":
                        try:
                            data = json.loads(body)
                            send_data = {
                                "action": "send",
                                "to": data.get("to"),
                                "body": data.get("body", {}),
                                "ttl_hours": data.get("ttl_hours"),
                            }
                            if data.get("attachments"):
                                send_data["attachments"] = data["attachments"]
                            res = await self._node.call_local("knarr-mail", send_data)
                            self._respond_json(writer, res)
                        except Exception as e:
                            self._respond_error(writer, 500, str(e))
                    elif path == "/api/messages/flush":
                        try:
                            data = json.loads(body) if body else {}
                            peer_id = data.get("peer")
                            if peer_id:
                                result = await self._node.force_heartbeat(peer_id)
                            else:
                                await self._node._sync.flush_outbox()
                                result = {"status": "ok", "action": "flush_all"}
                            self._respond_json(writer, result)
                        except Exception as e:
                            self._respond_error(writer, 500, str(e))
                    elif path == "/api/upload":
                        await self._handle_api_upload(writer, body, query)
                    elif path == "/api/assets":
                        self._handle_asset_store(writer, body, headers)
                    elif path == "/api/skills/install":
                        await self._handle_skill_install(writer, body)
                    elif path == "/api/exposures":
                        self._handle_exposure_create(writer, body)
                    elif path == "/api/pricing/discounts":
                        self._handle_pricing_discount_upsert(writer, body)
                    elif path == "/api/groups/refresh":
                        self._handle_groups_refresh(writer, body)
                    elif path == "/api/settlements/execute":
                        await self._handle_settlement_execute(writer, body)
                    elif path.startswith("/api/groups/") and path.endswith("/members"):
                        group_name = path[len("/api/groups/"):-len("/members")]
                        self._handle_group_member_manage(writer, group_name, body)
                    elif path == "/api/upgrade/check":
                        # v0.32.0: P1 — on-demand upgrade trigger (auth required)
                        result = await self._handle_upgrade_check()
                        self._respond_json(writer, result)
                    else:
                        self._respond_404(writer)
                elif method == "PUT":
                    if path.startswith("/api/exposures/") and path.count("/") == 3:
                        exp_name = path[len("/api/exposures/"):]
                        self._handle_exposure_update(writer, exp_name, body)
                    elif path.startswith("/api/secrets/") and path.count("/") == 4:
                        parts = path.split("/")
                        self._handle_secret_set(writer, parts[3], parts[4], body)
                    else:
                        self._respond_404(writer)
                elif method == "DELETE":
                    if path.startswith("/api/exposures/") and path.count("/") == 3:
                        exp_name = path[len("/api/exposures/"):]
                        self._handle_exposure_delete(writer, exp_name)
                    elif path.startswith("/api/assets/"):
                        asset_hash = path[len("/api/assets/"):]
                        self._handle_asset_delete(writer, asset_hash)
                    elif path.startswith("/api/secrets/") and path.count("/") == 4:
                        parts = path.split("/")
                        self._handle_secret_delete(writer, parts[3], parts[4])
                    elif path.startswith("/api/skills/") and not path.endswith("/schema"):
                        skill_name = path[len("/api/skills/"):]
                        await self._handle_skill_remove(writer, skill_name)
                    elif path.startswith("/api/groups/") and "/members/" in path:
                        # DELETE /api/groups/{name}/members/{node_id}
                        parts = path.split("/")
                        if len(parts) == 6:
                            group_name = parts[3]
                            node_id = parts[5]
                            self._handle_group_member_delete(writer, group_name, node_id)
                        else:
                            self._respond_404(writer)
                    elif path.startswith("/api/pricing/discounts/"):
                        discount_id = path[len("/api/pricing/discounts/"):]
                        self._handle_pricing_discount_delete(writer, discount_id)
                    else:
                        self._respond_404(writer)
                else:
                    self._respond_error(writer, 405, "Method Not Allowed")

            try:
                await asyncio.wait_for(_dispatch_request(), timeout=5.0)
            except asyncio.TimeoutError:
                self._respond_error(writer, 503, "Request timed out")

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            import traceback
            logger.error(f"Cockpit connection error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        finally:
            self._active_connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_api_execute(self, writer, body):
        """POST /api/execute — Execute a skill task."""
        if len(body) > 65536:
            self._respond_error(writer, 413, "Request Too Large")
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON")
            return

        skill = data.get("skill")
        task_input = data.get("input")
        if not skill or not isinstance(task_input, dict):
            self._respond_error(writer, 400, "Missing skill or invalid input")
            return

        provider = data.get("provider")
        # A5: normalize string provider to dict with node_id key
        if isinstance(provider, str):
            logger.debug(f"API_EXECUTE_PROVIDER_NORMALIZED: string -> dict node_id={provider[:16]!r}")
            provider = {"node_id": provider}
        local = data.get("local", False) is True

        timeout_ms = data.get("timeout_ms") or (int(data.get("timeout", 30)) * 1000)

        if local:
            try:
                # Async local execute via programmatic loopback
                result = await self._node.submit_async_task(
                    self._node.node_info.node_id, "127.0.0.1", self._node.node_info.port,
                    skill, task_input, timeout_ms=timeout_ms
                )
                if result.status in ("accepted", "queued"):
                    self._respond(writer, "202 Accepted", "application/json", json.dumps({
                        "status": result.status,
                        "job_id": result.task_id,
                        "position": getattr(result, "position", 0)
                    }).encode("utf-8"))
                elif result.status == "completed":
                    self._respond(writer, "200 OK", "application/json", json.dumps({
                        "status": "completed",
                        "job_id": result.task_id,
                    }).encode("utf-8"))
                else:
                    reason = getattr(result, "reason", "") or result.status
                    self._respond_error(writer, 409, f"Task rejected: {reason}")
            except Exception as e:
                logger.error(f"API local async execute failed: {e}")
                self._respond_error(writer, 500, "Task submission failed")
            return

        # B2: scored candidate selection when no explicit provider
        if not provider:
            provider = self._select_execute_provider(skill)

        if not provider:
            self._respond_error(writer, 404, "No provider found for skill")
            return

        # B2: if scored selection picked local, execute locally
        if provider.get("_local"):
            try:
                result = await self._node.submit_async_task(
                    self._node.node_info.node_id, "127.0.0.1", self._node.node_info.port,
                    skill, task_input, timeout_ms=timeout_ms
                )
                if result.status in ("accepted", "queued"):
                    self._respond(writer, "202 Accepted", "application/json", json.dumps({
                        "status": result.status,
                        "job_id": result.task_id,
                        "position": getattr(result, "position", 0)
                    }).encode("utf-8"))
                elif result.status == "completed":
                    self._respond(writer, "200 OK", "application/json", json.dumps({
                        "status": "completed",
                        "job_id": result.task_id,
                    }).encode("utf-8"))
                else:
                    reason = getattr(result, "reason", "") or result.status
                    self._respond_error(writer, 409, f"Task rejected: {reason}")
            except Exception as e:
                logger.error(f"API local async execute failed: {e}")
                self._respond_error(writer, 500, "Task submission failed")
            return

        # Resolve host/port from peer table when only node_id provided (V011-005)
        if provider.get("node_id") and not provider.get("host"):
            target_id = provider["node_id"]
            for peer in self._node.storage.get_peers():
                if peer.node_id == target_id:
                    provider["host"] = peer.host
                    provider["port"] = peer.port
                    break
            if not provider.get("host"):
                self._respond_error(writer, 404, f"Cannot resolve address for node {target_id[:16]}...")
                return

        try:
            result = await self._node.submit_async_task(
                provider["node_id"], provider["host"], provider["port"],
                skill, task_input, timeout_ms=timeout_ms
            )
            if result.status in ("accepted", "queued"):
                # Store local tracking entry for remote job
                expires_at = time.time() + 86400
                self._node.storage.insert_remote_job(
                    result.task_id, skill, provider["node_id"],
                    provider["host"], provider["port"], expires_at
                )
                self._respond(writer, "202 Accepted", "application/json", json.dumps({
                    "status": result.status,
                    "job_id": result.task_id,
                    "position": getattr(result, "position", 0)
                }).encode("utf-8"))
            elif result.status == "completed":
                expires_at = time.time() + 86400
                self._node.storage.insert_remote_job(
                    result.task_id, skill, provider["node_id"],
                    provider["host"], provider["port"], expires_at
                )
                self._respond(writer, "200 OK", "application/json", json.dumps({
                    "status": "completed",
                    "job_id": result.task_id,
                }).encode("utf-8"))
            else:
                reason = getattr(result, "reason", "") or result.status
                self._respond_error(writer, 409, f"Task rejected: {reason}")
        except Exception as e:
            logger.error(f"API remote async execute failed: {e}")
            self._respond_error(writer, 500, "Task submission failed")

    def _handle_asset_list(self, writer):
        """GET /api/assets — List local sidecar assets."""
        sidecar = getattr(self._node, '_sidecar', None)
        if not sidecar:
            self._respond_json(writer, {"assets": [], "total_size": 0, "enabled": False})
            return
        entries = []
        for h, m in sorted(sidecar._metadata.items()):
            entries.append({
                "hash": h,
                "size": m.size,
                "uploaded_at": m.uploaded_at,
                "access_count": m.access_count,
            })
            if len(entries) >= 1000:
                break
        self._respond_json(writer, {
            "assets": entries,
            "total_size": sidecar._total_size,
            "enabled": True,
        })

    def _handle_asset_store(self, writer, body: bytes, headers: dict):
        """POST /api/assets — Store binary data as a local asset. Returns knarr-asset:// URI."""
        if not hasattr(self._node, 'store_asset') or not getattr(self._node, '_asset_dir', ''):
            self._respond_error(writer, 400, "Sidecar not enabled")
            return
        if not body:
            self._respond_error(writer, 400, "Empty body")
            return
        content_type = headers.get("content-type", "")
        if "application/json" in content_type:
            # JSON mode: {files: [{data: "<base64>", name: "..."}, ...]}
            import base64 as b64mod
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._respond_error(writer, 400, "Invalid JSON")
                return
            files = data.get("files", [])
            if not isinstance(files, list) or not files:
                self._respond_error(writer, 400, "Missing 'files' array")
                return
            if len(files) > 20:
                self._respond_error(writer, 400, "Max 20 files per request")
                return
            results = []
            for f in files:
                if not isinstance(f, dict) or "data" not in f:
                    self._respond_error(writer, 400, "Each file must have 'data' field")
                    return
                try:
                    raw = b64mod.b64decode(f["data"], validate=True)
                except Exception:
                    self._respond_error(writer, 400, "Invalid base64 data")
                    return
                h = self._node.store_asset(raw)
                results.append({
                    "hash": h,
                    "uri": f"knarr-asset://{h}",
                    "size": len(raw),
                    "name": f.get("name", ""),
                })
            self._respond_json(writer, {"assets": results})
        else:
            # Raw binary mode: single file
            h = self._node.store_asset(body)
            self._respond_json(writer, {
                "assets": [{
                    "hash": h,
                    "uri": f"knarr-asset://{h}",
                    "size": len(body),
                }]
            })

    def _handle_asset_delete(self, writer, asset_hash):
        """DELETE /api/assets/{hash} — Delete a local asset."""
        if len(asset_hash) != 64 or not all(c in '0123456789abcdef' for c in asset_hash):
            self._respond_error(writer, 400, "Invalid hash")
            return
        asset_dir = getattr(self._node, '_asset_dir', '')
        if not asset_dir:
            self._respond_error(writer, 404, "Sidecar not enabled")
            return
        path = os.path.join(asset_dir, asset_hash)
        if not os.path.exists(path):
            self._respond_error(writer, 404, "Asset not found")
            return
        size = os.path.getsize(path)
        os.remove(path)
        sidecar = getattr(self._node, '_sidecar', None)
        if sidecar:
            sidecar._metadata.pop(asset_hash, None)
            sidecar._total_size -= size
        self._respond_json(writer, {"status": "ok"})

    async def _handle_asset_download(self, writer, asset_hash, query):
        """GET /api/assets/{hash} — Proxy asset download."""
        # P8A1-001 check
        if len(asset_hash) != 64 or not all(c in '0123456789abcdef' for c in asset_hash):
            self._respond_error(writer, 400, "Invalid hash")
            return

        # Try local first
        local_path = self._node.asset_path(asset_hash) if hasattr(self._node, 'asset_path') else None
        if local_path and os.path.exists(local_path):
            with open(local_path, "rb") as f:
                content = f.read()
            if hasattr(self._node, '_egress') and not self._node._egress.check_binary(content):
                self._respond_error(writer, 403, "SECURITY_VIOLATION")
                return
            self._respond_asset(writer, asset_hash, content)
            return

        # Try remote
        host = query.get("host", [""])[0]
        port = query.get("sidecar_port", ["0"])[0]
        if host and port.isdigit() and int(port) > 0:
            try:
                from ..cli.main import download_asset
                content = await download_asset(host, int(port), asset_hash, self._node._signing_key)
                if hasattr(self._node, '_egress') and not self._node._egress.check_binary(content):
                    self._respond_error(writer, 403, "SECURITY_VIOLATION")
                    return
                self._respond_asset(writer, asset_hash, content)
                return
            except Exception as e:
                logger.error(f"Proxy asset download failed: {type(e).__name__}")

        self._respond_404(writer)

    async def _handle_api_upload(self, writer, body, query):
        """POST /api/upload — Upload asset to local or remote sidecar."""
        host = query.get("host", [""])[0]
        port = query.get("sidecar_port", ["0"])[0]

        # Default to local sidecar if no host/port specified
        if not host or not port.isdigit() or int(port) <= 0:
            host = "127.0.0.1"
            port = str(self._node._sidecar_port or 0)
            if int(port) <= 0:
                self._respond_error(writer, 400, "Sidecar not enabled")
                return

        try:
            from ..cli.main import upload_asset
            asset_hash = await upload_asset(host, int(port), body, self._node._signing_key)
            self._respond_json(writer, {"hash": asset_hash})
        except Exception as e:
            logger.error(f"Proxy asset upload failed: {type(e).__name__}: {e}")
            self._respond_error(writer, 502, "Upload to sidecar failed")

    async def _handle_skill_install(self, writer, body):
        """POST /api/skills/install — Install a skill from source."""
        if len(body) > 4096:
            self._respond_error(writer, 413, "Request Too Large")
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON")
            return
        source = data.get("source", "").strip()
        if not source:
            self._respond_error(writer, 400, "Missing 'source' field")
            return
        # Basic input validation — prevent shell injection via source string
        if any(c in source for c in (";", "&", "|", "`", "$", "\n", "\r")):
            self._respond_error(writer, 400, "Invalid source path")
            return
        try:
            from ..cli.skill import cmd_skill_install
            config_dir = self._config_dir
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: cmd_skill_install(source, config_dir, force=data.get("force", False),
                                                upgrade=data.get("upgrade", False)))
            self._respond_json(writer, {"status": "ok", "message": result})
        except Exception as e:
            logger.error(f"Skill install failed: {type(e).__name__}: {e}")
            self._respond(writer, "400 Bad Request", "application/json",
                          json.dumps({"status": "error", "message": str(e)}).encode())

    async def _handle_skill_remove(self, writer, skill_name):
        """DELETE /api/skills/{name} — Remove an installed skill."""
        import re
        if not re.match(r'^[a-z0-9_-]+$', skill_name) or len(skill_name) > 64:
            self._respond_error(writer, 400, "Invalid skill name")
            return
        try:
            from ..cli.skill import cmd_skill_remove
            config_dir = self._config_dir
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: cmd_skill_remove(skill_name, config_dir, purge=False))
            self._respond_json(writer, {"status": "ok", "message": result})
        except Exception as e:
            logger.error(f"Skill remove failed: {type(e).__name__}: {e}")
            self._respond(writer, "400 Bad Request", "application/json",
                          json.dumps({"status": "error", "message": str(e)}).encode())

    def _handle_secret_set(self, writer, skill_name, key, body):
        """PUT /api/secrets/{skill}/{key} — Set a secret value."""
        import re
        if not re.match(r'^[a-z0-9_-]+$', skill_name) or not re.match(r'^[a-z0-9_]+$', key):
            self._respond_error(writer, 400, "Invalid skill name or key")
            return
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON")
            return
        value = data.get("value", "")
        if not isinstance(value, str):
            self._respond_error(writer, 400, "Value must be a string")
            return
        self._node.set_secret(skill_name, key, value)
        self._respond_json(writer, {"status": "ok"})

    def _handle_secret_delete(self, writer, skill_name, key):
        """DELETE /api/secrets/{skill}/{key} — Remove a secret."""
        import re
        if not re.match(r'^[a-z0-9_-]+$', skill_name) or not re.match(r'^[a-z0-9_]+$', key):
            self._respond_error(writer, 400, "Invalid skill name or key")
            return
        self._node.delete_secret(skill_name, key)
        self._respond_json(writer, {"status": "ok"})

    def _handle_exposure_list(self, writer):
        """GET /api/exposures — List all active exposures."""
        exposures = []
        for path, cfg in sorted(self._exposures.items()):
            entry = {
                "name": cfg["name"],
                "skill": cfg["skill"],
                "path": cfg["path"],
                "mode": cfg.get("mode", "auto"),
                "rate_limit": cfg["rate_limit"],
                "fields": list(cfg.get("fields", {}).keys()),
                "presets": list(cfg.get("presets", {}).keys()),
                "display": cfg.get("display", {}),
                "payment": cfg.get("payment", "none"),
            }
            exposures.append(entry)
        self._respond_json(writer, {"exposures": exposures})

    def _handle_exposure_create(self, writer, body):
        """POST /api/exposures — Create a new exposure."""
        import re
        if len(body) > 8192:
            self._respond_error(writer, 413, "Request Too Large")
            return
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON")
            return
        name = data.get("name", "").strip()
        if not name or not re.match(r'^[a-z0-9_-]+$', name) or len(name) > 64:
            self._respond_error(writer, 400, "Invalid exposure name")
            return
        skill = data.get("skill", "").strip()
        if not skill or not re.match(r'^[a-z0-9_-]+$', skill) or len(skill) > 64:
            self._respond_error(writer, 400, "Invalid skill name")
            return
        # C5: Validate skill exists (local handlers or network skills)
        known_skills = set(self._node._handlers.keys())
        for entry in self._node.storage.query_all_active_skills():
            known_skills.add(entry["skill_sheet"]["name"].lower())
        if skill.lower() not in known_skills:
            self._respond_error(writer, 400, f"Unknown skill '{skill}'")
            return
        path = data.get("path", name).strip()
        if not path or not re.match(r'^[a-z0-9_/-]+$', path) or len(path) > 128:
            self._respond_error(writer, 400, "Invalid path")
            return
        if path in self._exposures:
            self._respond_error(writer, 409, "Exposure path already exists")
            return
        auth_mode = data.get("auth", "none")
        tokens = data.get("tokens", [])
        if auth_mode == "token" and not tokens:
            self._respond_error(writer, 400, "auth=token requires at least one token")
            return
        mode = data.get("mode", "auto")
        if mode not in ("auto", "static"):
            self._respond_error(writer, 400, "mode must be 'auto' or 'static'")
            return
        cfg = {
            "name": name,
            "skill": skill,
            "path": path,
            "mode": mode,
            "presets": data.get("presets", {}),
            "fields": data.get("fields", {}),
            "display": data.get("display", {}),
            "provider": data.get("provider", {}),
            "rate_limit": int(data.get("rate_limit", 10)),
            "auth": auth_mode,
            "tokens": tokens,
            "max_calls_per_token": int(data.get("max_calls_per_token", 0)),
            "max_calls_per_day": int(data.get("max_calls_per_day", 0)),
            "timeout": max(1, min(int(data.get("timeout", 30)), 3600)),
            "timeout_ms": int(data.get("timeout_ms", 0)),
            # E-2: x402 payment schema (not enforced in v0.11.0)
            "payment": data.get("payment", "none"),
            "payment_amount": data.get("payment_amount", ""),
            "payment_asset": data.get("payment_asset", ""),
            "payment_network": data.get("payment_network", ""),
        }
        self._exposures[path] = cfg
        self._persist_exposures()
        self._respond_json(writer, {"status": "ok", "path": path})

    def _handle_exposure_update(self, writer, exp_name, body):
        """PUT /api/exposures/{name} — Update an existing exposure."""
        import re
        if not re.match(r'^[a-z0-9_-]+$', exp_name) or len(exp_name) > 64:
            self._respond_error(writer, 400, "Invalid exposure name")
            return
        # Find by name
        target_path = None
        for path, cfg in self._exposures.items():
            if cfg["name"] == exp_name:
                target_path = path
                break
        if target_path is None:
            self._respond_404(writer)
            return
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON")
            return
        cfg = self._exposures[target_path]
        if "skill" in data:
            cfg["skill"] = str(data["skill"]).strip()
        if "presets" in data and isinstance(data["presets"], dict):
            cfg["presets"] = data["presets"]
        if "fields" in data and isinstance(data["fields"], dict):
            cfg["fields"] = data["fields"]
        if "display" in data and isinstance(data["display"], dict):
            cfg["display"] = data["display"]
        if "provider" in data and isinstance(data["provider"], dict):
            cfg["provider"] = data["provider"]
        if "rate_limit" in data:
            cfg["rate_limit"] = int(data["rate_limit"])
        if "auth" in data:
            cfg["auth"] = str(data["auth"])
        if "tokens" in data and isinstance(data["tokens"], list):
            cfg["tokens"] = data["tokens"]
        if "max_calls_per_token" in data:
            cfg["max_calls_per_token"] = int(data["max_calls_per_token"])
        if "max_calls_per_day" in data:
            cfg["max_calls_per_day"] = int(data["max_calls_per_day"])
        if "mode" in data:
            cfg["mode"] = str(data["mode"]) if data["mode"] in ("auto", "static") else "auto"
        if "timeout" in data:
            cfg["timeout"] = max(1, min(int(data["timeout"]), 3600))
        if "timeout_ms" in data:
            cfg["timeout_ms"] = int(data["timeout_ms"])
        for pf in ("payment_address", "payment_network", "payment_amount"):
            if pf in data:
                cfg[pf] = str(data[pf])
        # Validate: auth=token requires tokens
        if cfg.get("auth") == "token" and not cfg.get("tokens"):
            self._respond_error(writer, 400, "auth=token requires at least one token")
            return
        # Handle path change
        new_path = data.get("path", "").strip()
        if new_path and new_path != target_path:
            if not re.match(r'^[a-z0-9_/-]+$', new_path) or len(new_path) > 128:
                self._respond_error(writer, 400, "Invalid path")
                return
            if new_path in self._exposures:
                self._respond_error(writer, 409, "Path already in use")
                return
            del self._exposures[target_path]
            cfg["path"] = new_path
            self._exposures[new_path] = cfg
        self._persist_exposures()
        self._respond_json(writer, {"status": "ok"})

    def _handle_exposure_delete(self, writer, exp_name):
        """DELETE /api/exposures/{name} — Remove an exposure."""
        import re
        if not re.match(r'^[a-z0-9_-]+$', exp_name) or len(exp_name) > 64:
            self._respond_error(writer, 400, "Invalid exposure name")
            return
        target_path = None
        for path, cfg in self._exposures.items():
            if cfg["name"] == exp_name:
                target_path = path
                break
        if target_path is None:
            self._respond_404(writer)
            return
        del self._exposures[target_path]
        self._rate_limits.pop(target_path, None)
        self._persist_exposures()
        self._respond_json(writer, {"status": "ok"})

    def _handle_pricing_discounts_list(self, writer):
        """GET /api/pricing/discounts — Return active pricing groups and rules."""
        try:
            conn = self._node.storage._get_conn()
            cursor = conn.execute("""
                SELECT id, name, group_name, skill_group, effect_pct, priority, 
                       active, created_at
                FROM pricing_discounts
                ORDER BY active DESC, priority DESC, created_at DESC
            """)
            discounts = [
                {
                    "id": r[0], "name": r[1], "group_name": r[2],
                    "skill_group": r[3], "effect_pct": r[4], "priority": r[5],
                    "active": bool(r[6]), "created_at": r[7]
                } for r in cursor.fetchall()
            ]
            
            # v0.28.0 also includes legacy config for visibility in the UI
            legacy_groups = self._node._config.get("pricing", {}).get("groups", {})
            legacy_discounts = self._node._config.get("pricing", {}).get("discounts", {})
            
            self._respond_json(writer, {
                "discounts": discounts,
                "legacy_config": {
                    "has_legacy": bool(legacy_discounts),
                    "groups": legacy_groups,
                    "discounts": legacy_discounts
                },
                "discount_mode": self._node._config.get("pricing", {}).get("discount_mode", "multiplicative")
            })
        except Exception as e:
            logger.error(f"Failed to list discounts: {e}")
            self._respond_error(writer, 500, str(e))

    def _handle_pricing_discount_upsert(self, writer, body):
        """POST /api/pricing/discounts — Create or update a discount rule."""
        import re
        try:
            data = json.loads(body)
            name = data.get("name", "").strip()
            group_name = data.get("group_name", "").strip()
            skill_group = data.get("skill_group", "*").strip()
            effect_pct = float(data.get("effect_pct", 0.0))
            priority = int(data.get("priority", 0))
            active = 1 if data.get("active", True) else 0

            if not name or not group_name:
                self._respond_error(writer, 400, "name and group_name are required")
                return

            # M-5: Length and charset validation
            for field_name, field_val in [("name", name), ("group_name", group_name), ("skill_group", skill_group)]:
                if len(field_val) > 128:
                    self._respond_error(writer, 400, f"{field_name} exceeds 128 characters")
                    return
                if not re.match(r'^[a-zA-Z0-9_\-\*\.]+$', field_val):
                    self._respond_error(writer, 400, f"{field_name} contains invalid characters")
                    return

            if effect_pct < 0 or effect_pct > 100:
                self._respond_error(writer, 400, "effect_pct must be between 0 and 100")
                return

            conn = self._node.storage._get_conn()

            if data.get("id"):
                # Update existing — M-4: reject updates to deactivated rows
                try:
                    discount_id = int(data["id"])
                except (ValueError, TypeError):
                    self._respond_error(writer, 400, "Invalid discount ID")
                    return
                existing = conn.execute("SELECT active FROM pricing_discounts WHERE id = ?", (discount_id,)).fetchone()
                if not existing:
                    self._respond_error(writer, 404, "Discount not found")
                    return
                if existing[0] == 0:
                    self._respond_error(writer, 409, "Cannot update deactivated discount")
                    return
                conn.execute("""
                    UPDATE pricing_discounts
                    SET name=?, group_name=?, skill_group=?, effect_pct=?,
                        priority=?
                    WHERE id=? AND active=1
                """, (name, group_name, skill_group, effect_pct, priority, discount_id))
            else:
                conn.execute("""
                    INSERT INTO pricing_discounts
                    (name, group_name, skill_group, effect_pct, priority, active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (name, group_name, skill_group, effect_pct, priority, active, time.time()))

            conn.commit()
            self._respond_json(writer, {"status": "ok"})
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON body")
        except Exception as e:
            logger.error(f"Failed to upsert discount: {e}")
            self._respond_error(writer, 500, "Internal error")

    def _handle_pricing_discount_delete(self, writer, discount_id: str):
        """DELETE /api/pricing/discounts/{id} — Deactivate a discount rule (soft delete)."""
        if not discount_id.isdigit() or len(discount_id) > 18:
            self._respond_error(writer, 400, "Invalid discount ID")
            return
        try:
            conn = self._node.storage._get_conn()
            conn.execute("UPDATE pricing_discounts SET active = 0 WHERE id = ?", (int(discount_id),))
            conn.commit()
            self._respond_json(writer, {"status": "deactivated"})
        except Exception as e:
            logger.error(f"Failed to deactivate discount: {e}")
            self._respond_error(writer, 500, "Internal error")

    def _persist_exposures(self):
        """Write current exposures to expose.toml in config_dir."""
        expose_path = os.path.join(self._config_dir, "expose.toml")
        lines = ["# Auto-generated by Knarr Cockpit — do not edit while running\n"]
        for path, cfg in sorted(self._exposures.items()):
            name = cfg["name"]
            lines.append(f"\n[{name}]")
            lines.append(f'skill = {json.dumps(cfg["skill"])}')
            if cfg["path"] != name:
                lines.append(f'path = {json.dumps(cfg["path"])}')
            if cfg["rate_limit"] != 10:
                lines.append(f'rate_limit = {cfg["rate_limit"]}')
            auth = cfg.get("auth", "none")
            if auth != "none":
                lines.append(f'auth = {json.dumps(auth)}')
            tokens = cfg.get("tokens", [])
            if tokens:
                lines.append(f'tokens = {json.dumps(tokens)}')
            if cfg.get("max_calls_per_token", 0) > 0:
                lines.append(f'max_calls_per_token = {cfg["max_calls_per_token"]}')
            if cfg.get("max_calls_per_day", 0) > 0:
                lines.append(f'max_calls_per_day = {cfg["max_calls_per_day"]}')
            presets = cfg.get("presets", {})
            if presets:
                lines.append("")
                lines.append(f"[{name}.presets]")
                for k, v in sorted(presets.items()):
                    lines.append(f'{k} = {json.dumps(v)}')
            fields = cfg.get("fields", {})
            if fields:
                for fk, fv in sorted(fields.items()):
                    lines.append("")
                    lines.append(f"[{name}.fields.{fk}]")
                    if isinstance(fv, dict):
                        for fvk, fvv in sorted(fv.items()):
                            lines.append(f'{fvk} = {json.dumps(fvv)}')
                    else:
                        lines.append(f'label = {json.dumps(str(fv))}')
            display = cfg.get("display", {})
            if display:
                lines.append("")
                lines.append(f"[{name}.display]")
                for dk, dv in sorted(display.items()):
                    lines.append(f'{dk} = {json.dumps(dv)}')
            mode = cfg.get("mode", "auto")
            if mode != "auto":
                lines.append(f'mode = {json.dumps(mode)}')
            timeout = cfg.get("timeout", 30)
            if timeout != 30:
                lines.append(f'timeout = {timeout}')
            timeout_ms = cfg.get("timeout_ms", 0)
            if timeout_ms > 0:
                lines.append(f'timeout_ms = {timeout_ms}')
            for pf in ("payment_address", "payment_network", "payment_amount"):
                if cfg.get(pf):
                    lines.append(f'{pf} = {json.dumps(cfg[pf])}')
            provider = cfg.get("provider", {})
            if provider:
                lines.append("")
                lines.append(f"[{name}.provider]")
                for pk, pv in sorted(provider.items()):
                    lines.append(f'{pk} = {json.dumps(pv)}')
            lines.append("")
        try:
            with open(expose_path, "w") as f:
                f.write("\n".join(lines))
            logger.info(f"Exposures saved to {expose_path}")
        except Exception as e:
            logger.error(f"Failed to persist exposures: {type(e).__name__}: {e}")

    def _respond_cors_preflight(self, writer):
        """Handle OPTIONS preflight for /s/ CORS requests."""
        header = (
            "HTTP/1.1 204 No Content\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type\r\n"
            "Access-Control-Max-Age: 86400\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header)

    def _respond_cors(self, writer, status, content_type, body):
        """Respond with CORS headers for /s/ endpoints."""
        header = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + body)

    def _respond_cors_json(self, writer, data):
        body = json.dumps(data).encode("utf-8")
        self._respond_cors(writer, "200 OK", "application/json", body)

    def _respond_cors_error(self, writer, code, message):
        self._respond_cors(writer, f"{code} {self._sanitize_status(message)}", "text/plain", message.encode("utf-8"))

    def _issue_x402_refund(self, x402_payment: dict) -> None:
        """v0.42.0 B4: Issue credit note refund after x402 execution failure."""
        if not x402_payment or x402_payment.get("refunded"):
            return
        payer_node_id = str(x402_payment.get("payer_node_id", "") or "").strip()
        if not payer_node_id:
            logger.warning("x402 refund skipped: missing payer_node_id")
            x402_payment["refunded"] = True
            return
        try:
            from ..commerce.conversion import get_conversion_rate, token_to_credits
            from ..commerce.documents import credit_note
            from ..commerce.handlers import _resolve_public_key
            from ..core.proof import sign_document

            if self._node._signing_key is None:
                raise RuntimeError("missing node signing key for refund")

            # Fix #5: convert token amount to credits for refund
            rate = get_conversion_rate(self._node._config)
            refund_credits = token_to_credits(float(x402_payment["charged_amount"]), rate)

            refund_doc = credit_note(
                provider=self._node.node_info.node_id,
                consumer=payer_node_id,
                amount=refund_credits,
                skill_name=x402_payment["skill_name"],
                reason="execution_failed",
                original_receipt_id=x402_payment["payment_receipt_id"],
            )
            signed_refund = sign_document(
                refund_doc.payload,
                self._node._signing_key,
                verification_method=f"did:knarr:{self._node.node_info.node_id}#key-1",
            )
            self._node._write_receipt(
                document_type="credit_note",
                payload=signed_refund,
                counterparty=payer_node_id,
                order_ref=x402_payment["payment_receipt_id"],
                proof_purpose="assertionMethod",
            )
            # Fix #3: resolve node_id → peer_public_key for ledger update
            peer_pubkey = _resolve_public_key(self._node, payer_node_id)
            if peer_pubkey:
                self._node.storage.update_ledger_refund(peer_pubkey, refund_credits)
            else:
                logger.warning("x402 refund ledger update failed: cannot resolve pubkey for %s", payer_node_id[:16])
            x402_payment["refunded"] = True
            logger.info(f"X402_REFUND issued for skill={x402_payment['skill_name']} amount={x402_payment['charged_amount']}")
        except Exception as exc:
            logger.error(f"X402_REFUND_FAILED: {type(exc).__name__}: {exc}")

    async def _route_exposure(self, method, path, body, query, writer, client_ip, headers=None):
        """Route /s/ requests to the appropriate exposure handler."""
        headers = headers or {}
        # CORS preflight
        if method == "OPTIONS":
            self._respond_cors_preflight(writer)
            return

        # Parse: /s/{exposure_path}[/execute|/schema|/assets/{hash}]
        suffix = path[3:]  # strip "/s/"
        if not suffix:
            self._respond_cors_error(writer, 404, "Not Found")
            return

        # Check for /assets/ sub-path
        parts = suffix.split("/")
        if len(parts) >= 3 and parts[-2] == "assets":
            # /s/{path}/assets/{hash}
            exposure_path = "/".join(parts[:-2])
            asset_hash = parts[-1]
            if exposure_path not in self._exposures:
                self._respond_cors_error(writer, 404, "Not Found")
                return
            if method != "GET":
                self._respond_cors_error(writer, 405, "Method Not Allowed")
                return
            await self._handle_asset_download(writer, asset_hash, query)
            return

        # Check for /execute or /schema suffix
        action = None
        job_id = None
        if suffix.endswith("/execute"):
            exposure_path = suffix[:-len("/execute")]
            action = "execute"
        elif suffix.endswith("/schema"):
            exposure_path = suffix[:-len("/schema")]
            action = "schema"
        elif "/status/" in suffix:
            parts = suffix.split("/status/")
            exposure_path = parts[0]
            job_id = parts[1]
            action = "status"
        elif "/result/" in suffix:
            parts = suffix.split("/result/")
            exposure_path = parts[0]
            job_id = parts[1]
            action = "result"
        else:
            exposure_path = suffix.rstrip("/")
            action = "page"

        exposure = self._exposures.get(exposure_path)

        # Check for knarr-static deployment if no configured exposure
        if not exposure:
            try:
                from ..static.handler import is_static_deployment, get_static_root
                # Check if any prefix of the path is a static deployment
                # e.g. /s/myapp/css/style.css -> deployment "myapp", subpath "css/style.css"
                candidate = exposure_path
                subpath = ""
                while candidate:
                    if is_static_deployment(candidate):
                        return self._serve_knarr_static(writer, candidate, subpath, method)
                    # Try parent path
                    if "/" in candidate:
                        last_slash = candidate.rfind("/")
                        subpath = candidate[last_slash+1:] + ("/" + subpath if subpath else "")
                        candidate = candidate[:last_slash]
                    else:
                        break
            except ImportError:
                pass
            self._respond_cors_error(writer, 404, "Not Found")
            return

        # Check if exposure uses static mode
        if exposure.get("mode") == "static" and action == "page" and method == "GET":
            static_path = exposure.get("static_path", exposure_path)
            try:
                from ..static.handler import is_static_deployment
                if is_static_deployment(static_path):
                    return self._serve_knarr_static(writer, static_path, "", method)
            except ImportError:
                pass

        if action == "page" and method == "GET":
            self._handle_exposure_page(writer, exposure)
        elif action == "schema" and method == "GET":
            self._handle_exposure_schema(writer, exposure)
        elif action == "execute" and method == "POST":
            # Token auth check
            token = self._check_exposure_auth(exposure, headers)
            if token is None:
                self._respond_cors_error(writer, 403, "Forbidden")
                return
            # IP-based rate limit
            if not self._check_rate_limit(exposure_path, client_ip):
                self._respond_cors_error(writer, 429, "Too Many Requests")
                return
            # Token-based rate limit
            if not self._check_token_rate_limit(exposure_path, token, exposure):
                self._respond_cors_error(writer, 429, "Too Many Requests")
                return
            await self._handle_exposure_execute(writer, body, exposure, headers)
        elif action == "status" and method == "GET":
            token = self._check_exposure_auth(exposure, headers)
            if token is None:
                self._respond_cors_error(writer, 403, "Forbidden")
                return
            await self._handle_job_status(writer, job_id)
        elif action == "result" and method == "GET":
            token = self._check_exposure_auth(exposure, headers)
            if token is None:
                self._respond_cors_error(writer, 403, "Forbidden")
                return
            await self._handle_job_result(writer, job_id)
        else:
            self._respond_cors_error(writer, 405, "Method Not Allowed")

    def _handle_exposure_page(self, writer, exposure):
        """GET /s/{path} — Generate standalone HTML form page with client-side schema rendering."""
        display = exposure.get("display", {})
        title = display.get("title", exposure["skill"])
        description = display.get("description", "")
        fields = exposure.get("fields", {})
        path = exposure["path"]

        # Build exposed field schema for client-side rendering
        # Prefer input_spec (full JSON Schema) over basic input_schema
        schema = self._node.get_skill_schema(exposure["skill"])
        input_spec = (schema.get("input_spec") or {}) if schema else {}
        input_spec_props = input_spec.get("properties", input_spec) if isinstance(input_spec, dict) else {}
        input_schema = schema.get("input_schema", {}) if schema else {}
        input_required = input_spec.get("required", []) if isinstance(input_spec, dict) else []
        exposed_schema = {}
        for field_name, field_cfg in fields.items():
            if not isinstance(field_cfg, dict):
                field_cfg = {"label": str(field_cfg)}
            # Use rich spec if available, fall back to basic type string
            if field_name in input_spec_props and isinstance(input_spec_props[field_name], dict):
                spec = dict(input_spec_props[field_name])
                if not spec.get("description") and field_cfg.get("description"):
                    spec["description"] = field_cfg["description"]
                if field_cfg.get("required") or field_name in input_required:
                    spec["required"] = True
                exposed_schema[field_name] = spec
            else:
                field_type = input_schema.get(field_name, "string")
                exposed_schema[field_name] = {
                    "type": field_type if isinstance(field_type, str) else "string",
                    "description": field_cfg.get("description", ""),
                    "required": field_cfg.get("required", False),
                }
        import json as _json
        schema_json = _json.dumps(exposed_schema)
        # Build field label mapping
        labels = {}
        for fn, fc in fields.items():
            if isinstance(fc, dict):
                labels[fn] = fc.get("label", fn)
            else:
                labels[fn] = str(fc)
        labels_json = _json.dumps(labels)

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;color:#333;padding:2rem}}
.container{{max-width:600px;margin:0 auto;background:#fff;border-radius:8px;padding:2rem;box-shadow:0 2px 8px rgba(0,0,0,0.1)}}
h1{{font-size:1.5rem;margin-bottom:0.5rem}}
.desc{{color:#666;margin-bottom:1.5rem}}
.field{{margin-bottom:1rem}}
.field label{{display:block;font-weight:600;margin-bottom:0.3rem;font-size:0.9rem}}
.field input,.field select,.field textarea{{width:100%;padding:0.5rem;border:1px solid #ddd;border-radius:4px;font-size:0.95rem}}
.field input[type=checkbox]{{width:auto}}
.field textarea{{resize:vertical}}
fieldset{{border:1px solid #ddd;border-radius:4px;padding:12px;margin-bottom:1rem}}
fieldset legend{{color:#666;font-size:0.85rem;padding:0 6px}}
.arr-list{{margin-bottom:6px}}
.arr-row{{display:flex;gap:6px;margin-bottom:4px}}
.arr-row input{{flex:1}}
.arr-rm{{background:#e74c3c;color:#fff;border:none;border-radius:4px;padding:4px 8px;cursor:pointer}}
.arr-add{{background:#2563eb;color:#fff;border:none;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:0.85rem;margin-bottom:8px}}
.btn{{display:inline-block;padding:0.6rem 1.5rem;background:#2563eb;color:#fff;border:none;border-radius:4px;font-size:1rem;cursor:pointer}}
.btn:hover{{background:#1d4ed8}}
.btn:disabled{{opacity:0.5;cursor:not-allowed}}
.result{{margin-top:1.5rem;padding:1rem;border-radius:4px}}
.result-ok{{background:#f0fdf4;border:1px solid #86efac}}
.result-err{{background:#fef2f2;border:1px solid #fca5a5;color:#991b1b}}
.result pre{{white-space:pre-wrap;word-break:break-word;font-size:0.9rem;margin-top:0.5rem}}
.result-meta{{color:#666;font-size:0.8rem;margin-top:0.5rem}}
.download{{display:inline-block;margin-top:0.5rem;color:#2563eb;text-decoration:underline}}
.loading{{display:none;margin-left:0.5rem}}
.loading.active{{display:inline}}
.helper{{color:#888;font-size:0.8rem;margin-top:2px}}
</style>
</head>
<body>
<div class="container">
<h1>{_html_escape(title)}</h1>
{"<p class='desc'>" + _html_escape(description) + "</p>" if description else ""}
<form id="skillForm">
<div id="formFields"></div>
<button type="submit" class="btn" id="submitBtn">Submit</button>
<span class="loading" id="spinner">Processing...</span>
</form>
<div id="result"></div>
</div>
<script>
var SCHEMA={schema_json};
var LABELS={labels_json};
function esc(s){{if(s==null)return'';var d=document.createElement('div');d.appendChild(document.createTextNode(String(s)));return d.innerHTML;}}
function renderField(k,spec,prefix){{
if(typeof spec==='string')spec={{type:spec}};
var t=(spec.type||'string').toLowerCase(),desc=spec.description||'',req=spec.required||false;
var id='field-'+(prefix||'')+k,lab=LABELS[k]||k.replace(/_/g,' ');
var rm=req?' <span style="color:#e74c3c">*</span>':'';
var dh=desc?'<small class="helper">'+esc(desc)+'</small>':'';
var ra=req?' required':'';
var dv=spec['default']!==undefined?spec['default']:'';
var ph=spec.examples&&spec.examples.length?esc(String(spec.examples[0])):'';
if(t==='bool'||t==='boolean')return'<div class="field"><label><input type="checkbox" id="'+esc(id)+'"'+ra+(dv?' checked':'')+'>  '+esc(lab)+rm+'</label>'+dh+'</div>';
if(t==='number'||t==='int'||t==='float'||t==='integer')return'<div class="field"><label>'+esc(lab)+rm+'</label><input type="number" id="'+esc(id)+'" step="'+(t==='int'||t==='integer'?'1':'any')+'" value="'+esc(String(dv))+'" placeholder="'+ph+'"'+ra+'>'+dh+'</div>';
if(spec['enum']&&Array.isArray(spec['enum'])){{var s='<div class="field"><label>'+esc(lab)+rm+'</label><select id="'+esc(id)+'"'+ra+'><option value="">-- Select --</option>';spec['enum'].forEach(function(o){{var sel=String(o)===String(dv)?' selected':'';s+='<option value="'+esc(String(o))+'"'+sel+'>'+esc(String(o))+'</option>'}});return s+'</select>'+dh+'</div>';}}
if(t.indexOf('enum:')===0){{var s='<div class="field"><label>'+esc(lab)+rm+'</label><select id="'+esc(id)+'"'+ra+'><option value="">-- Select --</option>';t.substring(5).split(',').forEach(function(o){{s+='<option value="'+esc(o.trim())+'">'+esc(o.trim())+'</option>'}});return s+'</select>'+dh+'</div>';}}
if(t==='object'&&spec.properties){{var h='<fieldset><legend>'+esc(lab)+rm+'</legend>';if(desc)h+='<small class="helper" style="display:block;margin-bottom:8px">'+esc(desc)+'</small>';for(var pk in spec.properties)h+=renderField(pk,spec.properties[pk],(prefix||'')+k+'.');return h+'</fieldset>';}}
if(t==='array'){{var it=spec.items_type||spec.items||'string';return'<div class="field"><label>'+esc(lab)+rm+'</label><div id="'+esc(id)+'-list" class="arr-list" data-item-type="'+esc(String(typeof it==='object'?'string':it))+'"></div><button type="button" class="arr-add" data-add-list="'+esc(id)+'-list">+ Add</button>'+dh+'</div>';}}
var isLong=/text|description|content|prompt|body|message/i.test(k);
if(isLong)return'<div class="field"><label>'+esc(lab)+rm+'</label><textarea id="'+esc(id)+'" rows="4" placeholder="'+ph+'"'+ra+'>'+esc(String(dv))+'</textarea>'+dh+'</div>';
return'<div class="field"><label>'+esc(lab)+rm+'</label><input type="text" id="'+esc(id)+'" value="'+esc(String(dv))+'" placeholder="'+ph+'"'+ra+'>'+dh+'</div>';
}}
function buildForm(){{
var c=document.getElementById('formFields'),h='';
if(!SCHEMA||!Object.keys(SCHEMA).length){{c.innerHTML='<div class="field"><label>Input (JSON)</label><textarea id="field-__raw_json" rows="6" placeholder="{{}}"></textarea></div>';return;}}
for(var k in SCHEMA)h+=renderField(k,SCHEMA[k],'');
c.innerHTML=h;
c.querySelectorAll('[data-add-list]').forEach(function(btn){{
btn.addEventListener('click',function(){{
var list=document.getElementById(btn.dataset.addList);if(!list)return;
var it=list.dataset.itemType||'string',div=document.createElement('div');
div.className='arr-row';div.innerHTML='<input type="'+(it==='number'?'number':'text')+'" class="array-item"><button type="button" class="arr-rm">\\u00d7</button>';
div.querySelector('.arr-rm').addEventListener('click',function(){{div.remove()}});
list.appendChild(div);
}});
}});
}}
function collectValues(schema,prefix){{
if(!schema||!Object.keys(schema).length){{var raw=document.getElementById('field-__raw_json');if(raw)try{{return JSON.parse(raw.value||'{{}}')}}catch(e){{return{{}}}}return{{}};}}
var data={{}};
for(var k in schema){{
var s=typeof schema[k]==='string'?{{type:schema[k]}}:schema[k];
var t=(s.type||'string').toLowerCase(),id='field-'+(prefix||'')+k;
if(t==='object'&&s.properties){{data[k]=collectValues(s.properties,(prefix||'')+k+'.');}}
else if(t==='array'){{var list=document.getElementById(id+'-list');var it=s.items_type||s.items||'string';data[k]=list?Array.from(list.querySelectorAll('.array-item')).map(function(el){{return it==='number'?(el.value===''?0:Number(el.value)):el.value}}).filter(function(v){{return v!==''}}):[]; }}
else{{var el=document.getElementById(id);if(!el)continue;if(el.type==='checkbox')data[k]=el.checked;else if(el.type==='number')data[k]=el.value===''?0:(t==='int'||t==='integer'?parseInt(el.value):parseFloat(el.value));else data[k]=el.value;}}
}}
return data;
}}
buildForm();
document.getElementById('skillForm').addEventListener('submit',async function(e){{
e.preventDefault();
var btn=document.getElementById('submitBtn'),sp=document.getElementById('spinner'),rd=document.getElementById('result');
btn.disabled=true;sp.classList.add('active');rd.innerHTML='';
var data=collectValues(SCHEMA,'');
try{{
var r=await fetch('/s/{_html_escape(path)}/execute',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data)}});

if(r.status===202){{
    var j=await r.json();
    var poll=async function(){{
        var sr=await fetch('/s/{_html_escape(path)}/status/'+j.job_id);
        var sj=await sr.json();
        if(sj.status==='completed'||sj.status==='failed'){{
            var rr=await fetch('/s/{_html_escape(path)}/result/'+j.job_id);
            var rj=await rr.json();
            renderFinalResult(rj);
            btn.disabled=false;sp.classList.remove('active');
            return;
        }}
        rd.innerHTML='<div class="result result-ok">Queued (position '+sj.position+')...</div>';
        setTimeout(poll,5000);
    }};
    poll();
    return;
}}

var j=await r.json();
renderFinalResult(j);

}}catch(err){{rd.innerHTML='<div class="result result-err">Request failed</div>';}}
btn.disabled=false;sp.classList.remove('active');
}});

function renderFinalResult(j){{
var rd=document.getElementById('result');
if(j.status==='completed'){{
var html='<div class="result result-ok">';
if(j.output_data){{
var txt=JSON.stringify(j.output_data,null,2);
var fmt='{_html_escape(display.get("result_format", "text"))}';
if(fmt==='json')html+='<pre>'+esc(txt)+'</pre>';
else{{
var vals=Object.values(j.output_data);
html+='<pre>'+vals.map(function(v){{return esc(typeof v==='string'?v:JSON.stringify(v))}}).join('\\n')+'</pre>';
}}
for(var k in j.output_data){{
var v=j.output_data[k];
if(typeof v==='string'&&v.startsWith('knarr-asset://')){{
var h=v.replace('knarr-asset://','');
if(/^[0-9a-f]{{64}}$/.test(h))html+='<a class="download" href="/s/{_html_escape(path)}/assets/'+h+'" download>Download '+esc(k)+'</a><br>';
}}
}}
}}
if(j.wall_time_ms)html+='<div class="result-meta">Completed in '+j.wall_time_ms+'ms</div>';
html+='</div>';rd.innerHTML=html;
}}else{{
rd.innerHTML='<div class="result result-err"><strong>Error</strong><pre>'+esc(j.error&&j.error.message?j.error.message:'Task failed')+'</pre></div>';
}}
}}
</script>
</body>
</html>"""
        self._respond_cors(writer, "200 OK", "text/html; charset=utf-8", page.encode("utf-8"))

    def _serve_knarr_static(self, writer, deployment_path: str, subpath: str, method: str):
        """Serve a file from a knarr-static deployment."""
        if method != "GET":
            self._respond_cors_error(writer, 405, "Method Not Allowed")
            return
        try:
            from ..static.handler import get_static_root
            static_root = get_static_root()
            deployment_dir = static_root / deployment_path

            # Determine which file to serve
            if not subpath or subpath.endswith("/"):
                file_rel = (subpath or "") + "index.html"
            else:
                file_rel = subpath

            file_path = (deployment_dir / file_rel).resolve()

            # Path confinement
            if not str(file_path).startswith(str(deployment_dir.resolve()) + os.sep) and \
               file_path != deployment_dir.resolve():
                self._respond_cors_error(writer, 403, "Forbidden")
                return

            if not file_path.is_file():
                self._respond_cors_error(writer, 404, "Not Found")
                return

            ext = file_path.suffix.lower()
            content_type = MIME_TYPES.get(ext, "application/octet-stream")
            with open(file_path, "rb") as f:
                data = f.read()
            self._respond_cors(writer, "200 OK", content_type, data)
        except Exception:
            self._respond_cors_error(writer, 404, "Not Found")

    def _handle_exposure_schema(self, writer, exposure):
        """GET /s/{path}/schema — Return exposed fields for custom frontends."""
        display = exposure.get("display", {})
        fields = exposure.get("fields", {})
        schema = self._node.get_skill_schema(exposure["skill"])
        input_schema = schema.get("input_schema", {}) if schema else {}

        exposed = {}
        for field_name, field_cfg in fields.items():
            if not isinstance(field_cfg, dict):
                field_cfg = {"label": str(field_cfg)}
            exposed[field_name] = {
                "label": field_cfg.get("label", field_name),
                "type": input_schema.get(field_name, "string"),
                "required": field_cfg.get("required", False),
            }

        self._respond_cors_json(writer, {
            "title": display.get("title", exposure["skill"]),
            "description": display.get("description", ""),
            "fields": exposed,
        })

    async def _handle_exposure_execute(self, writer, body, exposure, headers=None):
        """POST /s/{path}/execute — Validate, merge presets, execute."""
        if len(body) > 65536:
            self._respond_cors_error(writer, 413, "Request Too Large")
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond_cors_error(writer, 400, "Invalid JSON")
            return

        if not isinstance(data, dict):
            self._respond_cors_error(writer, 400, "Invalid input")
            return

        headers = headers or {}
        payment_mode = exposure.get("payment", "none")
        x402_payment = None
        if payment_mode != "none":
            payment_sig = headers.get("payment-signature", "")
            chain_cfg = self._node._chain
            expected_amount = exposure.get("payment_amount") or exposure.get("price") or 0
            expected_asset = exposure.get("payment_asset") or chain_cfg.get("token_mint", "")
            expected_dest = exposure.get("payment_address", "")
            node_address = self._node._wallet
            # Fail closed: reject if asset or destination not configured
            if not expected_asset or not expected_dest:
                logger.error(f"x402 config incomplete: asset={expected_asset!r} dest={expected_dest!r}")
                self._respond_cors_error(writer, 500, "Payment configuration incomplete: payment_asset and payment_address required")
                return
            if not payment_sig:
                try:
                    from ..commerce.x402 import build_payment_required

                    pr = build_payment_required(chain_cfg, exposure, node_address)
                except Exception as exc:
                    logger.error(f"x402 build failed: {type(exc).__name__}: {exc}")
                    self._respond_cors_error(writer, 500, "Payment configuration invalid")
                    return
                self._respond_cors(
                    writer,
                    "402 Payment Required",
                    "application/json",
                    json.dumps(pr).encode("utf-8"),
                )
                return
            try:
                import base64 as _b64, hashlib as _hl
                from ..commerce.x402 import verify_x402_payload, settle_x402

                # Persistent replay check — survives restarts, no TTL gap
                try:
                    _raw = _b64.b64decode(payment_sig, validate=True)
                    _digest = _hl.sha256(_raw).hexdigest()
                except Exception:
                    self._respond_cors_error(writer, 403, "payment-signature must be base64 transaction bytes")
                    return
                if self._node.storage.check_payment_receipt(_digest):
                    self._respond_cors_error(writer, 403, "replay detected")
                    return

                verify_result = verify_x402_payload(
                    payment_sig,
                    expected_amount,
                    expected_asset,
                    expected_dest,
                    node_address,
                )
                if not verify_result.get("verified"):
                    self._respond_cors_error(writer, 403, verify_result.get("error", "Invalid payment"))
                    return

                settle_result = await settle_x402(
                    verify_result["tx_bytes"],
                    getattr(self._node, "_signing_key", None),
                    chain_cfg.get("rpc_url", ""),
                )
                if not settle_result.get("success"):
                    self._respond_cors_error(writer, 502, settle_result.get("error", "Payment settlement failed"))
                    return
                self._node.storage.store_payment_receipt(
                    _digest,
                    verify_result["amount"],
                    verify_result["asset"],
                    verify_result["destination"],
                )
                x402_payment = {
                    "charged_amount": verify_result["amount"],
                    "payer_node_id": str(
                        data.get("payer_node_id")
                        or data.get("peer_key")
                        or data.get("peer_public_key")
                        or ""
                    ).strip(),
                    "payment_receipt_id": _digest,
                    "skill_name": str(exposure.get("skill", "") or ""),
                }
            except Exception as exc:
                logger.error(f"x402 verify/settle failed: {type(exc).__name__}: {exc}")
                self._respond_cors_error(writer, 502, "Payment verification failed")
                return

        fields = exposure.get("fields", {})
        presets = exposure.get("presets", {})

        # Validate: reject unknown keys
        allowed_keys = set(fields.keys())
        extra = set(data.keys()) - allowed_keys
        if extra:
            self._issue_x402_refund(x402_payment)
            self._respond_cors_error(writer, 400, f"Unknown fields: {', '.join(sorted(extra))}")
            return

        # Validate: required fields present
        for field_name, field_cfg in fields.items():
            if isinstance(field_cfg, dict) and field_cfg.get("required", False):
                if field_name not in data or data[field_name] == "":
                    self._issue_x402_refund(x402_payment)
                    self._respond_cors_error(writer, 400, f"Missing required field: {field_name}")
                    return

        # Merge: presets first, then user input; presets win on conflict
        task_input = dict(data)
        task_input.update(presets)

        skill = exposure["skill"]
        provider_cfg = exposure.get("provider", {})
        strategy = provider_cfg.get("strategy", "first")
        # Fix D: Use timeout_ms from exposure config
        timeout_ms = exposure.get("timeout_ms") or (exposure.get("timeout", 30) * 1000)

        # Check local first
        if hasattr(self._node, '_handlers') and skill.lower() in self._node._handlers:
            try:
                # v0.17.0: Fix C — Always use async execution for cockpit
                result = await self._node.submit_async_task(
                    self._node.node_info.node_id, "127.0.0.1", self._node.node_info.port,
                    skill, task_input, timeout_ms=timeout_ms
                )
                if result.status in ("accepted", "queued"):
                    self._respond_cors(writer, "202 Accepted", "application/json", json.dumps({
                        "job_id": result.task_id,
                        "status": result.status,
                        "queue_position": getattr(result, "position", 0),
                        "estimated_wait_ms": getattr(result, "position", 0) * 5000
                    }).encode("utf-8"))
                elif result.status == "completed":
                    self._respond_cors(writer, "200 OK", "application/json", json.dumps({
                        "job_id": result.task_id,
                        "status": "completed",
                    }).encode("utf-8"))
                else:
                    reason = getattr(result, "reason", "") or result.status
                    self._issue_x402_refund(x402_payment)
                    self._respond_cors(writer, "409 Conflict", "application/json", json.dumps({
                        "status": "failed",
                        "error": {"code": "TASK_REJECTED", "message": f"Task rejected: {reason}"}
                    }).encode("utf-8"))
            except Exception as e:
                logger.error(f"Exposure local async execute failed: {type(e).__name__}: {e}")
                self._issue_x402_refund(x402_payment)
                self._respond_cors_json(writer, {
                    "status": "failed",
                    "error": {"code": "HANDLER_ERROR", "message": "Task submission failed"}
                })
            return

        # Remote execution — resolve provider
        provider = None
        all_skills = self._node.get_skills()
        candidates = []
        for s in all_skills["network"]:
            if s["name"].lower() == skill.lower():
                candidates = s.get("providers", [])
                break

        if strategy == "specific":
            target_id = provider_cfg.get("node_id", "")
            for p in candidates:
                if p["node_id"] == target_id:
                    provider = p
                    break
        elif strategy == "cheapest":
            if candidates:
                provider = min(candidates, key=lambda p: (
                    p.get("price", 1.0), p.get("load", 10)))
        elif strategy == "jurisdiction":
            target_j = provider_cfg.get("jurisdiction", "")
            strict = provider_cfg.get("jurisdiction_strict", True)
            for c in candidates:
                if target_j in (c.get("jurisdiction") or []):
                    provider = c
                    break
            if not provider:
                if strict:
                    self._issue_x402_refund(x402_payment)
                    self._respond_cors_error(writer, 404,
                        f"No provider found in jurisdiction '{target_j}'")
                    return
                elif candidates:
                    provider = candidates[0]
        else:
            # "first" strategy (default)
            if candidates:
                provider = candidates[0]

        if not provider:
            self._issue_x402_refund(x402_payment)
            self._respond_cors_error(writer, 404, "No provider found")
            return

        try:
            # v0.17.0: Fix C — Always use async execution for cockpit
            result = await self._node.submit_async_task(
                provider["node_id"], provider["host"], provider["port"],
                skill, task_input, timeout_ms=timeout_ms
            )
            if result.status in ("accepted", "queued"):
                # Store local tracking entry for remote job
                expires_at = time.time() + 86400
                self._node.storage.insert_remote_job(
                    result.task_id, skill, provider["node_id"],
                    provider["host"], provider["port"], expires_at
                )
                self._respond_cors(writer, "202 Accepted", "application/json", json.dumps({
                    "job_id": result.task_id,
                    "status": result.status,
                    "queue_position": getattr(result, "position", 0),
                    "estimated_wait_ms": getattr(result, "position", 0) * 5000
                }).encode("utf-8"))
            elif result.status == "completed":
                expires_at = time.time() + 86400
                self._node.storage.insert_remote_job(
                    result.task_id, skill, provider["node_id"],
                    provider["host"], provider["port"], expires_at
                )
                self._respond_cors(writer, "200 OK", "application/json", json.dumps({
                    "job_id": result.task_id,
                    "status": "completed",
                }).encode("utf-8"))
            else:
                reason = getattr(result, "reason", "") or result.status
                self._issue_x402_refund(x402_payment)
                self._respond_cors(writer, "409 Conflict", "application/json", json.dumps({
                    "status": "failed",
                    "error": {"code": "TASK_REJECTED", "message": f"Task rejected: {reason}"}
                }).encode("utf-8"))
        except Exception as e:
            logger.error(f"Exposure async execute failed: {type(e).__name__}: {e}")
            self._issue_x402_refund(x402_payment)
            self._respond_cors_json(writer, {
                "status": "failed",
                "error": {"code": "EXECUTION_ERROR", "message": "Task submission failed"}
            })

    async def _handle_job_status(self, writer, job_id: str):
        """GET /s/{path}/status/{job_id} — Query job status."""
        job = self._node.storage.get_async_job(job_id)
        if not job:
            self._respond_cors_error(writer, 404, "Job not found")
            return
        
        resp = {
            "job_id": job["job_id"],
            "status": job["status"],
            "position": job["position"],
            "updated_at": job["updated_at"]
        }
        if job.get("provider_node_id"):
            resp["provider_node_id"] = job["provider_node_id"]
        self._respond_cors_json(writer, resp)

    async def _handle_job_result(self, writer, job_id: str):
        """GET /s/{path}/result/{job_id} — Retrieve job result."""
        job = self._node.storage.get_async_job(job_id)
        if not job:
            self._respond_cors_error(writer, 404, "Job not found")
            return
        
        # Elder review: 410 Gone for expired jobs
        if job["status"] == "expired":
            self._respond_cors(writer, "410 Gone", "application/json", json.dumps({
                "status": "expired",
                "message": "Job result expired after grace period"
            }).encode("utf-8"))
            return

        if job["status"] != "completed" and job["status"] != "failed":
            # BUG-28: Return JSON (not plain text) for running jobs — let client decide retry
            self._respond_cors(writer, "200 OK", "application/json",
                json.dumps({"job_id": job["job_id"], "status": job["status"]}).encode())
            return

        resp = {
            "job_id": job["job_id"],
            "status": job["status"],
            "output_data": job["result"],
            "error": job["error"]
        }
        
        # Before returning skill response to caller:
        if hasattr(self._node, '_egress'):
            response_str = json.dumps(resp["output_data"]) if resp["output_data"] else ""
            if response_str and not self._node._egress.check(response_str):
                self._respond_cors_json(writer, {"error": "SECURITY_VIOLATION", "code": "EGRESS_FILTER_BLOCKED"})
                return

        self._respond_cors_json(writer, resp)

    def _check_auth(self, headers: dict, source_ip: str = "", endpoint: str = "") -> bool:
        if not self._auth_token:
            return True
        auth = headers.get("authorization", "")
        expected = f"Bearer {self._auth_token}"
        result = hmac.compare_digest(auth.encode(), expected.encode())
        if not result:
            # v0.33.0: security.auth_failed
            _bus = getattr(self._node, 'bus', None)
            if _bus:
                _bus.emit("security.auth_failed", source_ip=(source_ip or "")[:20], endpoint=(endpoint or "")[:40])
        return result

    def _check_wallet_auth(
        self, method: str, path: str, body: bytes | str, headers: dict
    ) -> bool:
        """v0.38.0 A3.2: HMAC authentication for wallet write operations.

        Verifies X-Wallet-Signature header using:
            HMAC-SHA256(send_secret, timestamp + "\\n" + method + "\\n" + path + "\\n" + body)

        Also verifies X-Wallet-Timestamp is within ±30 seconds.
        """
        import hashlib as _hashlib
        wallet_cfg = (getattr(self._node, '_config', None) or {}).get("cockpit", {}).get("wallet", {})
        send_secret = wallet_cfg.get("send_secret", "")
        if not send_secret:
            # No secret configured — reject all wallet write requests
            logger.warning("WALLET_AUTH_FAIL: send_secret not configured")
            return False

        timestamp_window = int(wallet_cfg.get("timestamp_window_seconds", 30))

        # Get and validate timestamp
        ts_header = headers.get("x-wallet-timestamp", "")
        if not ts_header:
            logger.warning("WALLET_AUTH_FAIL: missing X-Wallet-Timestamp")
            return False
        try:
            ts = int(ts_header)
        except (ValueError, TypeError):
            logger.warning(f"WALLET_AUTH_FAIL: unparseable timestamp {ts_header!r}")
            return False

        if abs(time.time() - ts) > timestamp_window:
            logger.warning(
                f"WALLET_AUTH_FAIL: timestamp {ts} outside window "
                f"(now={int(time.time())} window={timestamp_window}s)"
            )
            return False

        # Get signature from header
        sig_header = headers.get("x-wallet-signature", "")
        if not sig_header:
            logger.warning("WALLET_AUTH_FAIL: missing X-Wallet-Signature")
            return False

        # Build body string for signing
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = body or ""

        # Compute expected HMAC
        msg = f"{ts}\n{method}\n{path}\n{body_str}"
        expected_sig = hmac.new(
            send_secret.encode("utf-8"),
            msg.encode("utf-8"),
            _hashlib.sha256,
        ).hexdigest()

        # Constant-time compare
        if not hmac.compare_digest(sig_header.encode(), expected_sig.encode()):
            logger.warning("WALLET_AUTH_FAIL: signature mismatch")
            return False

        # FIX-002: Replay guard — reject duplicate (timestamp, signature) tuples
        sig_key = f"{ts}:{sig_header}"
        now_replay = time.time()
        if sig_key in self._seen_wallet_sigs:
            logger.warning("WALLET_AUTH_FAIL: replayed signature")
            return False
        # Record signature with expiry (window + margin)
        self._seen_wallet_sigs[sig_key] = now_replay + timestamp_window + 5
        # Periodic sweep of expired entries
        if now_replay - self._last_sig_sweep > self._sig_sweep_interval:
            self._seen_wallet_sigs = {
                k: v for k, v in self._seen_wallet_sigs.items() if v > now_replay
            }
            self._last_sig_sweep = now_replay

        return True

    def _check_wallet_spend_cap(self, amount: float) -> tuple[bool, str]:
        """v0.38.0 A3.3: Check per-tx and daily spending caps.

        Returns (allowed, reason).
        """
        wallet_cfg = (getattr(self._node, '_config', None) or {}).get("cockpit", {}).get("wallet", {})
        max_per_tx = float(wallet_cfg.get("max_per_tx", 100.0))
        max_daily = float(wallet_cfg.get("max_daily", 1000.0))

        # Per-tx cap
        if amount > max_per_tx:
            return False, f"amount {amount} exceeds per-tx cap {max_per_tx}"

        # Daily rolling cap: reset if > 24h since last reset
        now = time.time()
        if now - self._wallet_daily_reset > 86400:
            self._wallet_daily_spent = 0.0
            self._wallet_daily_reset = now

        if self._wallet_daily_spent + amount > max_daily:
            return False, (
                f"daily cap exceeded: spent={self._wallet_daily_spent:.2f} "
                f"+ requested={amount:.2f} > max={max_daily:.2f}"
            )

        return True, ""

    def _serve_static(self, path: str) -> tuple[bytes, str]:
        """Returns (content_bytes, content_type). Raises FileNotFoundError."""
        if path == "/" or path == "" or path == "/index.html":
            filename = "index.html"
        else:
            filename = path.lstrip("/")

        if ".." in filename or "/" in filename:
            raise FileNotFoundError()

        CONTENT_TYPES = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
        }

        ext = os.path.splitext(filename)[1]
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

        # Dev mode: serve from filesystem
        dev_path = os.environ.get("KNARR_COCKPIT_DEV")
        if dev_path:
            file_path = os.path.join(dev_path, filename)
            if os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    return f.read(), content_type

        # PyInstaller bundle: sys._MEIPASS contains extracted data files
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            bundled = os.path.join(meipass, "knarr", "dashboard", filename)
            if os.path.isfile(bundled):
                with open(bundled, "rb") as f:
                    return f.read(), content_type

        # Production: serve from package resources
        try:
            import importlib.resources
            ref = importlib.resources.files("knarr.dashboard").joinpath(filename)
            return ref.read_bytes(), content_type
        except (ImportError, AttributeError, FileNotFoundError, TypeError):
            pass

        # Fallback: serve relative to this file (editable installs)
        fallback = os.path.join(os.path.dirname(__file__), filename)
        if os.path.isfile(fallback):
            with open(fallback, "rb") as f:
                return f.read(), content_type

        raise FileNotFoundError()

    def _respond(self, writer, status, content_type, body):
        header = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header + body)

    def _respond_json(self, writer, data):
        body = json.dumps(data).encode("utf-8")
        self._respond(writer, "200 OK", "application/json", body)

    def _respond_404(self, writer):
        self._respond(writer, "404 Not Found", "text/plain", b"Not Found")

    def _respond_401(self, writer):
        self._respond(writer, "401 Unauthorized", "text/plain", b"Unauthorized")

    @staticmethod
    def _sanitize_status(s: str) -> str:
        """Strip control characters from HTTP status text to prevent response splitting."""
        return s.replace("\r", "").replace("\n", "").replace("\0", "")

    _MAGIC_TABLE = [
        (b'\x89PNG\r\n\x1a\n', None, '.png', 'image/png'),
        (b'\xff\xd8\xff', None, '.jpg', 'image/jpeg'),
        (b'GIF87a', None, '.gif', 'image/gif'),
        (b'GIF89a', None, '.gif', 'image/gif'),
        (b'%PDF', None, '.pdf', 'application/pdf'),
        (b'PK\x03\x04', None, '.zip', 'application/zip'),
        (b'\x1f\x8b', None, '.gz', 'application/gzip'),
        (b'OggS', None, '.ogg', 'audio/ogg'),
        (b'ID3', None, '.mp3', 'audio/mpeg'),
        (b'\xff\xfb', None, '.mp3', 'audio/mpeg'),
        (b'\x1aE\xdf\xa3', None, '.webm', 'video/webm'),
        (b'RIFF', b'WAVE', '.wav', 'audio/wav'),
    ]

    @staticmethod
    def _sniff_asset(data: bytes) -> tuple:
        """Guess (extension, mime_type) from content. Returns ('.bin', 'application/octet-stream') as fallback."""
        for magic, extra, ext, mime in CockpitServer._MAGIC_TABLE:
            if data[:len(magic)] == magic:
                if extra is None or (len(data) > 8 and data[8:8+len(extra)] == extra):
                    return ext, mime
        # MP4/MOV: ftyp box at offset 4
        if len(data) > 8 and data[4:8] == b'ftyp':
            return '.mp4', 'video/mp4'
        # Text/JSON heuristic: starts with { or [ and is valid UTF-8
        if data and data[0:1] in (b'{', b'['):
            try:
                data[:1024].decode('utf-8')
                return '.json', 'application/json'
            except UnicodeDecodeError:
                pass
        # Plain text heuristic
        if data and all(b < 128 for b in data[:512]):
            try:
                data[:1024].decode('utf-8')
                return '.txt', 'text/plain'
            except UnicodeDecodeError:
                pass
        return '.bin', 'application/octet-stream'

    def _respond_asset(self, writer, asset_hash: str, content: bytes):
        """Respond with asset content and Content-Disposition filename."""
        ext, mime = self._sniff_asset(content)
        filename = f"{asset_hash[:16]}{ext}"
        # Inline display for images/text/JSON; attachment for everything else
        disposition = "inline" if mime.startswith(("image/", "text/", "application/json")) else "attachment"
        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {mime}\r\n"
            f"Content-Length: {len(content)}\r\n"
            f"Content-Disposition: {disposition}; filename=\"{filename}\"\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + content)

    def _respond_error(self, writer, code, message):
        self._respond(writer, f"{code} {self._sanitize_status(message)}", "text/plain", message.encode("utf-8"))

    # ── Group API (v0.26.0) ─────────────────────────────────────

    def _handle_groups_list(self, writer):
        """GET /api/groups — list all groups with member counts."""
        engine = getattr(self._node, '_group_engine', None)
        if not engine:
            self._respond_json(writer, [])
            return
        groups = []
        cache = getattr(engine, '_cache', {})
        defs = getattr(engine, '_group_defs', {})
        for name, members in cache.items():
            gtype = defs.get(name, {}).get("type", "explicit")
            groups.append({"name": name, "type": gtype, "members": len(members)})
        self._respond_json(writer, groups)

    def _handle_group_members(self, writer, group_name: str):
        """GET /api/groups/<name>/members — list members of a group."""
        engine = getattr(self._node, '_group_engine', None)
        if not engine:
            self._respond_json(writer, [])
            return
        cache = getattr(engine, '_cache', {})
        members = cache.get(group_name, set())
        self._respond_json(writer, sorted(members))

    @staticmethod
    def _validate_node_id_hex(node_id: str) -> bool:
        """Validate node_id is exactly 64 hex characters."""
        if not node_id or len(node_id) != 64:
            return False
        try:
            bytes.fromhex(node_id)
            return True
        except ValueError:
            return False

    def _handle_group_member_manage(self, writer, group_name: str, body: bytes):
        """POST /api/groups/<name>/members — add/remove member (explicit groups only)."""
        import json
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._respond_error(writer, 400, "Invalid JSON")
            return
        action = data.get("action", "add")
        node_id = data.get("node_id", "")
        if not self._validate_node_id_hex(node_id):
            self._respond_error(writer, 400, "Invalid node_id (must be 64 hex chars)")
            return
        engine = getattr(self._node, '_group_engine', None)
        if not engine:
            self._respond_error(writer, 503, "GroupEngine not available")
            return
        try:
            if action == "add":
                added = engine.add_member(group_name, node_id)
                status = "added" if added else "already_member"
            elif action == "remove":
                removed = engine.remove_member(group_name, node_id)
                status = "removed" if removed else "not_member"
            else:
                self._respond_error(writer, 400, f"Unknown action: {action}")
                return
        except ValueError as e:
            self._respond_error(writer, 400, str(e))
            return
        cache = getattr(engine, '_cache', {})
        count = len(cache.get(group_name, set()))
        self._respond_json(writer, {"status": status, "member_count": count})

    def _handle_group_member_delete(self, writer, group_name: str, node_id: str):
        """DELETE /api/groups/{name}/members/{node_id}"""
        if not self._validate_node_id_hex(node_id):
            self._respond_error(writer, 400, "Invalid node_id (must be 64 hex chars)")
            return
        engine = getattr(self._node, '_group_engine', None)
        if not engine:
            self._respond_error(writer, 503, "GroupEngine not available")
            return
        try:
            removed = engine.remove_member(group_name, node_id)
            status = "removed" if removed else "not_member"
        except ValueError as e:
            self._respond_error(writer, 400, str(e))
            return
        cache = getattr(engine, '_cache', {})
        count = len(cache.get(group_name, set()))
        self._respond_json(writer, {"status": status, "member_count": count})

    def _handle_groups_refresh(self, writer, body: bytes):
        """POST /api/groups/refresh — force re-evaluate all groups."""
        engine = getattr(self._node, '_group_engine', None)
        if not engine:
            self._respond_error(writer, 503, "GroupEngine not available")
            return
        import json
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}
        group_name = data.get("name")
        if hasattr(engine, 'refresh'):
            engine.refresh(group_name)
        self._respond_json(writer, {"status": "ok"})

    def _handle_logs(self, writer, query: dict):
        """GET /api/logs?limit=100 — return recent in-memory logs."""
        try:
            limit = int(query.get("limit", ["100"])[0])
        except Exception:
            limit = 100
        limit = max(1, min(limit, 1000))
        logs = self._log_handler.tail(limit)
        self._respond_json(writer, {"logs": logs, "count": len(logs)})

    async def _handle_settlement_execute(self, writer, body: bytes):
        """POST /api/settlements/execute — execute a countersigned settlement."""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond_error(writer, 400, "Invalid JSON")
            return

        peer_key = str(data.get("peer_key", "")).strip()
        prepared_doc = data.get("prepared_doc")
        countersigned_doc = data.get("countersigned_doc")
        if len(peer_key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in peer_key):
            self._respond_error(writer, 400, "Invalid peer_key")
            return
        if not isinstance(prepared_doc, dict) or not isinstance(countersigned_doc, dict):
            self._respond_error(writer, 400, "prepared_doc and countersigned_doc are required")
            return

        try:
            from ..commerce.settlement_execution import execute_settlement

            authority_vm = str(countersigned_doc.get("proof", {}).get("verificationMethod", ""))
            authority_signing_key = getattr(self._node, "_cockpit_signing_key", None)
            if authority_vm.endswith("#thrall-1"):
                authority_signing_key = getattr(self._node, "_thrall_signing_key", authority_signing_key)
            authority_verify_key = (
                authority_signing_key.verify_key
                if authority_signing_key is not None
                else self._node._signing_key.verify_key
            )

            receipt_id = await execute_settlement(
                prepared_doc=prepared_doc,
                countersigned_doc=countersigned_doc,
                node_verify_key=self._node._signing_key.verify_key,
                authority_verify_key=authority_verify_key,
                node_id=self._node.node_info.node_id,
                signing_key=self._node._signing_key,
                peer_key=peer_key,
                storage=self._node.storage,
                send_mail_fn=self._node._sync.enqueue,
                bus=getattr(self._node, "bus", None),
                config=self._node._config,
            )
        except ValueError as exc:
            self._respond_error(writer, 400, str(exc))
            return
        except Exception as exc:
            logger.error(f"Settlement execute failed: {type(exc).__name__}: {exc}")
            self._respond_error(writer, 500, "Settlement execution failed")
            return

        self._respond_json(writer, {"status": "ok", "receipt_id": receipt_id})

    async def _handle_receipt_fetch(self, writer, reference: str):
        """GET /api/receipts/{reference} — fetch signed credit note by job_id.

        v0.32.0: The issuer is the source of truth. Only the counterparty (recipient)
        can fetch a receipt. Auth is via the cockpit token (operator access only).
        """
        # Basic reference validation — job_ids are UUIDs (36 chars) or hex strings
        if not reference or len(reference) > 128:
            self._respond_error(writer, 400, "Invalid reference")
            return
        try:
            note_json = self._node.storage.get_credit_note_by_reference(reference)
            if not note_json:
                self._respond_json(writer, {"status": "not_found"})
                return
            self._respond_json(writer, {"status": "ok", "credit_note": note_json})
        except Exception as e:
            logger.error(f"Receipt fetch error ref={reference}: {e}")
            self._respond_error(writer, 500, "Internal error")

    async def _handle_upgrade_check(self) -> dict:
        """POST /api/upgrade/check — trigger immediate upgrade check.

        v0.32.0: P1 — on-demand upgrade check, same as the periodic background check.
        Requires cockpit auth token (enforced by the dispatch layer).
        Returns current version + available version if any.
        """
        try:
            from knarr.dht.upgrade import get_latest_version
            from knarr import __version__
            available = get_latest_version()
            return {
                "status": "ok",
                "current_version": __version__,
                "available_version": available,
                "upgrade_available": available is not None and available != __version__,
            }
        except Exception as e:
            logger.warning(f"UPGRADE_CHECK_FAIL: {e}")
            return {"status": "error", "error": str(e)}
