"""knarr-groups plugin: GroupEngine implementation with explicit and computed groups."""
import hashlib
import logging
import operator
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set

from knarr.dht.plugins import PluginHooks

log = logging.getLogger(__name__)

# Operator mapping for predicate parsing
_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

# Regex to parse predicates like ">= 3", "< 0.5"
_PRED_RE = re.compile(r"^\s*(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)\s*$")


class GroupsPlugin(PluginHooks):
    """GroupEngine plugin — provides explicit and computed group membership."""

    def __init__(self, ctx, config: dict):
        self._ctx = ctx
        self._config = config
        self._node_config: Dict[str, Any] = {}  # set from ctx later
        self._debug = config.get("debug", False)
        self._eval_interval = float(config.get("eval_interval", 300))
        self._last_eval = 0.0
        self._cache: Dict[str, Set[str]] = {}  # group_name -> set of member IDs
        self._group_defs: Dict[str, Dict[str, Any]] = {}  # group_name -> config
        self._db_conn: Optional[sqlite3.Connection] = None
        self._db_path: Optional[str] = None

        # Load explicit groups into cache immediately
        self._load_explicit_groups()

        # Register self as the active GroupEngine
        ctx.group_engine = self
        if self._debug:
            ctx.log.info(f"GRP_INIT groups={len(self._cache)} eval_interval={self._eval_interval}")

    # ── GroupEngine interface ──────────────────────────────

    def is_member(self, item_id: str, group: str) -> bool:
        """Synchronous O(1) cache lookup. Firewall hot path safe."""
        return item_id in self._cache.get(group, set())

    def get_groups(self, item_id: str) -> List[str]:
        """Return all groups containing this item. Synchronous."""
        return [g for g, members in self._cache.items() if item_id in members]

    def add_member(self, group_name: str, node_id: str) -> bool:
        """Add a member to an explicit group. Returns True if added, False if already present."""
        gdef = self._group_defs.get(group_name, {})
        gtype = gdef.get("type", "explicit")
        if gtype != "explicit":
            raise ValueError(f"Cannot mutate {gtype} group '{group_name}'")
        if group_name not in self._cache:
            self._cache[group_name] = set()
        if node_id in self._cache[group_name]:
            return False
        self._cache[group_name].add(node_id)
        self._persist_members(group_name)
        self._ctx.log.info(f"group_mutation: add {node_id[:16]} -> {group_name}")
        return True

    def remove_member(self, group_name: str, node_id: str) -> bool:
        """Remove a member from an explicit group. Returns True if removed."""
        gdef = self._group_defs.get(group_name, {})
        gtype = gdef.get("type", "explicit")
        if gtype != "explicit":
            raise ValueError(f"Cannot mutate {gtype} group '{group_name}'")
        if group_name not in self._cache:
            return False
        if node_id not in self._cache[group_name]:
            return False
        self._cache[group_name].discard(node_id)
        self._persist_members(group_name)
        self._ctx.log.info(f"group_mutation: remove {node_id[:16]} -> {group_name}")
        return True

    def _persist_members(self, group_name: str):
        """Write group members to members file."""
        members = sorted(self._cache.get(group_name, set()))
        gdef = self._group_defs.get(group_name, {})
        members_file = gdef.get("members_file")
        plugin_dir = str(self._ctx.plugin_dir)
        if not members_file:
            members_file = os.path.join(plugin_dir, f"{group_name}.members")
        else:
            members_file = os.path.join(plugin_dir, members_file) if not os.path.isabs(members_file) else members_file
        # Path confinement check
        real = os.path.realpath(members_file)
        plugin_dir_abs = os.path.realpath(plugin_dir)
        if not (real.startswith(plugin_dir_abs + os.sep) or real == plugin_dir_abs):
            raise ValueError(f"Members file path escapes data directory: {members_file}")
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w") as f:
            for m in members:
                f.write(m + "\n")

    # ── PluginHooks ────────────────────────────────────────

    async def on_connect(self, peer_ip: str) -> bool:
        return True

    async def on_inbound(self, msg, peer_ip: str) -> bool:
        return True

    async def on_outbound(self, msg, peer) -> bool:
        return True

    async def on_tick(self, peers, health) -> None:
        """Periodically re-evaluate computed groups."""
        now = time.monotonic()
        if now - self._last_eval >= self._eval_interval:
            self._last_eval = now
            self._evaluate_all_computed()

    async def on_query(self, query_type: str, value: str) -> list:
        return []

    async def on_shutdown(self) -> None:
        if self._db_conn:
            try:
                self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None

    # ── Config loading ─────────────────────────────────────

    def _load_explicit_groups(self):
        """Load explicit group members into cache from config."""
        groups_cfg = self._config.get("groups", {})

        for name, cfg in groups_cfg.items():
            if not isinstance(cfg, dict):
                continue
            gtype = cfg.get("type", "explicit")
            self._group_defs[name] = cfg

            if gtype == "explicit":
                members = set(cfg.get("members", []))
                # members_file handling: path confinement via plugin_dir
                mf = cfg.get("members_file")
                if mf:
                    plugin_dir = str(self._ctx.plugin_dir)
                    mf_path = os.path.join(plugin_dir, mf) if not os.path.isabs(mf) else mf
                    mf_path = os.path.abspath(mf_path)
                    plugin_dir_abs = os.path.abspath(plugin_dir)
                    if not (mf_path.startswith(plugin_dir_abs + os.sep) or mf_path == plugin_dir_abs):
                        log.warning(f"Group '{name}' members_file escapes plugin directory: {mf}")
                    elif os.path.exists(mf_path):
                        try:
                            with open(mf_path) as f:
                                for line in f:
                                    line = line.strip()
                                    if line and not line.startswith("#"):
                                        members.add(line)
                        except OSError as e:
                            log.warning(f"Group '{name}' members_file error: {e}")
                self._cache[name] = members
                if self._debug:
                    self._ctx.log.info(f"GRP_LOAD name={name} type=explicit members={len(members)}")

    # ── Computed group evaluation ──────────────────────────

    def _get_db(self) -> Optional[sqlite3.Connection]:
        """Get read-only SQLite connection to node.db for computed queries."""
        if self._db_conn is not None:
            return self._db_conn
        db_path = self._ctx.storage_path
        if not db_path or not os.path.exists(str(db_path)):
            return None
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            conn.execute("PRAGMA busy_timeout=3000")
            self._db_conn = conn
            return conn
        except Exception as e:
            log.warning(f"Groups plugin: cannot open node.db: {e}")
            return None

    def _evaluate_all_computed(self):
        """Re-evaluate all computed and DHT groups, refresh externals."""
        for name, cfg in self._group_defs.items():
            gtype = cfg.get("type", "explicit")
            try:
                if gtype == "computed":
                    filters = cfg.get("filter", {})
                    if not filters:
                        continue
                    members = self._evaluate_computed(name, filters)
                    self._cache[name] = members
                    if self._debug:
                        self._ctx.log.info(f"GRP_EVAL name={name} type=computed members={len(members)}")
                elif gtype == "dht":
                    match = cfg.get("match", {})
                    if not match:
                        continue
                    members = self._evaluate_dht_group(name, match)
                    self._cache[name] = members
                    if self._debug:
                        self._ctx.log.info(f"GRP_EVAL name={name} type=dht members={len(members)}")
                elif gtype == "external":
                    source = cfg.get("source", "")
                    if source == "https":
                        members = self._fetch_external_https(name, cfg)
                        self._cache[name] = members
                        if self._debug:
                            self._ctx.log.info(f"GRP_EVAL name={name} type=external members={len(members)}")
            except Exception as e:
                log.warning(f"Groups plugin: eval failed for '{name}': {e}")

    def _evaluate_computed(self, group_name: str, filters: dict) -> Set[str]:
        """Evaluate filter predicates against local storage."""
        conn = self._get_db()
        if conn is None:
            return set()

        # Start with all known peers from ledger.
        # Identity domain: ledger stores peer_public_key (Ed25519 hex),
        # but execution_log stores caller_node_id (SHA-256 of public_key).
        # We build a pubkey→node_id map so filters can work across both tables.
        pk_to_nid: Dict[str, str] = {}
        try:
            cursor = conn.execute("SELECT peer_public_key FROM ledger")
            for (pk,) in cursor.fetchall():
                nid = hashlib.sha256(bytes.fromhex(pk)).hexdigest()
                pk_to_nid[pk] = nid
        except Exception:
            return set()

        if not pk_to_nid:
            return set()

        # Candidates are node_ids (the universal identity key)
        candidates = set(pk_to_nid.values())

        for field, predicate in filters.items():
            if not candidates:
                break

            if field in ("skill_name", "skill_prefix"):
                # String filter — not a numeric predicate
                candidates = self._filter_by_skill(conn, candidates, field, str(predicate))
                continue

            # Numeric predicate filter
            match = _PRED_RE.match(str(predicate))
            if not match:
                log.warning(f"Groups: invalid predicate for {group_name}.{field}: {predicate!r}")
                continue
            op_func = _OPS[match.group(1)]
            threshold = float(match.group(2))

            if field == "task_count":
                candidates = self._filter_task_count(conn, candidates, op_func, threshold)
            elif field == "ledger_balance":
                # Balance filter uses ledger table (keyed by public_key)
                # Convert candidates back to public_keys for lookup, then back to node_ids
                nid_to_pk = {v: k for k, v in pk_to_nid.items()}
                pk_candidates = {nid_to_pk[nid] for nid in candidates if nid in nid_to_pk}
                pk_result = self._filter_balance(conn, pk_candidates, op_func, threshold)
                candidates = {pk_to_nid[pk] for pk in pk_result if pk in pk_to_nid}
            elif field == "last_trade_days":
                # Same as balance — ledger is keyed by public_key
                nid_to_pk = {v: k for k, v in pk_to_nid.items()}
                pk_candidates = {nid_to_pk[nid] for nid in candidates if nid in nid_to_pk}
                pk_result = self._filter_last_trade_days(conn, pk_candidates, op_func, threshold)
                candidates = {pk_to_nid[pk] for pk in pk_result if pk in pk_to_nid}
            else:
                log.warning(f"Groups: unknown filter field '{field}' in group '{group_name}'")

        return candidates

    def _filter_task_count(self, conn, candidates: Set[str], op_func, threshold: float) -> Set[str]:
        """Filter by task count from execution_log."""
        result = set()
        for peer in candidates:
            try:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM execution_log WHERE caller_node_id = ?",
                    (peer,)
                )
                row = cursor.fetchone()
                if row and op_func(row[0], threshold):
                    result.add(peer)
            except Exception:
                pass
        return result

    def _filter_balance(self, conn, candidates: Set[str], op_func, threshold: float) -> Set[str]:
        """Filter by ledger balance."""
        result = set()
        for peer in candidates:
            try:
                cursor = conn.execute(
                    "SELECT balance FROM ledger WHERE peer_public_key = ?",
                    (peer,)
                )
                row = cursor.fetchone()
                if row and op_func(row[0], threshold):
                    result.add(peer)
            except Exception:
                pass
        return result

    def _filter_last_trade_days(self, conn, candidates: Set[str], op_func, threshold: float) -> Set[str]:
        """Filter by days since last trade."""
        now = time.time()
        result = set()
        for peer in candidates:
            try:
                cursor = conn.execute(
                    "SELECT last_updated FROM ledger WHERE peer_public_key = ?",
                    (peer,)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    days_ago = (now - row[0]) / 86400.0
                    if op_func(days_ago, threshold):
                        result.add(peer)
            except Exception:
                pass
        return result

    def _filter_by_skill(self, conn, candidates: Set[str], field: str, value: str) -> Set[str]:
        """Filter by skill name (exact) or skill prefix (LIKE)."""
        result = set()
        for peer in candidates:
            try:
                if field == "skill_name":
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM execution_log WHERE caller_node_id = ? AND skill_name = ?",
                        (peer, value)
                    )
                elif field == "skill_prefix":
                    # Prefix match using LIKE with escaped value
                    like_val = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM execution_log WHERE caller_node_id = ? AND skill_name LIKE ? ESCAPE '\\'",
                        (peer, like_val)
                    )
                else:
                    continue
                row = cursor.fetchone()
                if row and row[0] > 0:
                    result.add(peer)
            except Exception:
                pass
        return result

    # ── DHT-Computed Groups (v0.26.0) ──────────────────────────

    def _evaluate_dht_group(self, name: str, match: dict) -> Set[str]:
        """Evaluate DHT-computed group from peer announcement data.

        Note: Only ONE match criterion per group (elif chain). Use separate
        groups for multi-criteria filtering, then intersect in policy config.
        """
        conn = self._get_db()
        if conn is None:
            return set()

        candidates = set()

        # Jurisdiction match (v0.27.0: supports comma-separated multi-jurisdiction)
        if "jurisdiction" in match:
            target = match["jurisdiction"]
            from knarr.dht.storage import Storage
            rows = conn.execute("SELECT node_id, jurisdiction FROM peers").fetchall()
            for r in rows:
                if target in Storage.parse_jurisdiction(r[1]):
                    candidates.add(r[0])

        # Skill prefix match
        elif "skill_prefix" in match:
            prefix = match["skill_prefix"]
            like_val = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = conn.execute(
                "SELECT DISTINCT provider_node_id FROM skills WHERE skill_key LIKE ? ESCAPE '\\'",
                (like_val,)
            ).fetchall()
            candidates = {r[0] for r in rows}

        # Wallet match (has_wallet = true)
        elif "has_wallet" in match and match["has_wallet"]:
            rows = conn.execute(
                "SELECT node_id FROM peers WHERE wallet != '' AND wallet IS NOT NULL"
            ).fetchall()
            candidates = {r[0] for r in rows}

        # Encryption match (has_encryption = true)
        elif "has_encryption" in match and match["has_encryption"]:
            rows = conn.execute(
                "SELECT node_id FROM peers WHERE encryption_key != '' AND encryption_key IS NOT NULL"
            ).fetchall()
            candidates = {r[0] for r in rows}

        return candidates

    # ── External HTTPS+Signature Source (v0.26.0) ──────────────

    def _fetch_external_https(self, name: str, cfg: dict) -> Set[str]:
        """Fetch signed group membership list from HTTPS endpoint."""
        import urllib.request
        import json as _json

        url = cfg.get("url", "")
        verify_key_str = cfg.get("verify_key", "")
        refresh_interval = int(cfg.get("refresh_interval", 3600))

        # Validate URL scheme (SSRF mitigation)
        if not url.startswith("https://"):
            log.warning(f"Group '{name}': only HTTPS URLs supported, got: {url[:50]}")
            return getattr(self, f"_ext_cache_{name}", set())

        # Check cache freshness
        cache_key = f"_ext_cache_{name}"
        cache_ts_key = f"_ext_ts_{name}"
        cached_ts = getattr(self, cache_ts_key, 0)
        if time.time() - cached_ts < refresh_interval:
            return getattr(self, cache_key, set())

        try:
            from nacl.signing import VerifyKey
            from nacl.exceptions import BadSignatureError

            # Fetch (no-redirect handler — S-07 SSRF mitigation)
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise urllib.error.HTTPError(newurl, code, "redirect blocked", headers, fp)

            opener = urllib.request.build_opener(_NoRedirect)
            req = urllib.request.Request(url, headers={"User-Agent": "knarr-groups/0.26.0"})
            with opener.open(req, timeout=30) as resp:
                body = resp.read(1024 * 1024)  # 1MB cap
                sig_header = resp.headers.get("X-Knarr-Signature", "")

            # Verify signature
            if not sig_header:
                log.warning(f"Group '{name}': no X-Knarr-Signature header from {url}")
                return getattr(self, cache_key, set())

            if verify_key_str.startswith("ed25519:"):
                key_hex = verify_key_str[8:]
                vk = VerifyKey(bytes.fromhex(key_hex))
                vk.verify(body, bytes.fromhex(sig_header))
            else:
                log.warning(f"Group '{name}': unsupported verify_key format")
                return getattr(self, cache_key, set())

            # Parse
            data = _json.loads(body)
            if isinstance(data, list):
                members = {str(item) for item in data if isinstance(item, str) and len(item) == 64}
            else:
                log.warning(f"Group '{name}': expected JSON array, got {type(data).__name__}")
                return getattr(self, cache_key, set())

            # Update cache
            setattr(self, cache_key, members)
            setattr(self, cache_ts_key, time.time())
            if self._debug:
                self._ctx.log.info(f"GRP_EXT_FETCH name={name} members={len(members)}")
            return members

        except BadSignatureError:
            log.warning(f"Group '{name}': signature verification failed for {url}")
            return getattr(self, cache_key, set())
        except Exception as e:
            log.warning(f"Group '{name}': fetch failed: {e}")
            return getattr(self, cache_key, set())  # stale cache on failure

    def refresh(self, group_name: str = None):
        """Force refresh computed/external groups. Called from CLI/API."""
        if group_name:
            cfg = self._group_defs.get(group_name)
            if not cfg:
                return
            gtype = cfg.get("type", "explicit")
            if gtype == "computed":
                filters = cfg.get("filter", {})
                if filters:
                    self._cache[group_name] = self._evaluate_computed(group_name, filters)
            elif gtype == "dht":
                match = cfg.get("match", {})
                if match:
                    self._cache[group_name] = self._evaluate_dht_group(group_name, match)
            elif gtype == "external" and cfg.get("source") == "https":
                setattr(self, f"_ext_ts_{group_name}", 0)
                self._cache[group_name] = self._fetch_external_https(group_name, cfg)
            elif gtype == "explicit":
                # Re-read from config + members file (preserves API mutations)
                self._load_explicit_groups()
        else:
            self._evaluate_all_computed()
            self._load_explicit_groups()

