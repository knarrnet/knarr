import json
import sqlite3
import time
import logging
from typing import List, Dict, Any, Optional
from ..core.models import NodeInfo, SkillSheet, Task, LedgerEntry

logger = logging.getLogger(__name__)

MAX_PEERS = 1000  # SA-04
MAX_SKILLS = 5000  # SA-04
MAX_LEDGER_ENTRIES = 10000
MAX_DEMAND_ENTRIES = 1000

# v0.29.1: Mail bucket tables — fixed set, never user-supplied
# v0.32.0: Added mail_creditnote for signed credit notes
MAIL_BUCKETS = {"mail_inbox", "mail_jobreport", "mail_system", "mail_creditnote"}

class Storage:
    """Handles persistence of peers and skills using SQLite."""

    @staticmethod
    def parse_jurisdiction(jur_str: str) -> list:
        """Parse comma-separated jurisdiction string to list."""
        if not jur_str:
            return []
        return [j.strip() for j in jur_str.split(",") if j.strip()]

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        # For :memory: databases, we must keep one connection open to keep it alive.
        # check_same_thread=False: handlers run in thread pool threads (SA-9a-001).
        self._keepalive_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._keepalive_conn.execute("PRAGMA journal_mode=WAL")
        self._keepalive_conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    def _init_db(self):
        cursor = self._get_conn().cursor()
        # Peers table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS peers (
                node_id TEXT PRIMARY KEY,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                last_seen REAL NOT NULL
            )
        """)
        # Skills table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                skill_key TEXT,
                provider_node_id TEXT,
                skill_record_json TEXT NOT NULL,
                announced_at REAL NOT NULL,
                ttl INTEGER NOT NULL,
                is_own INTEGER DEFAULT 0,
                provider_public_key TEXT,
                announce_signature TEXT,
                provider_msg_id TEXT,
                PRIMARY KEY (skill_key, provider_node_id)
            )
        """)
        # Node identity table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS node_identity (
                key_bytes BLOB
            )
        """)
        # Tasks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                skill_name TEXT NOT NULL,
                requester_node_id TEXT NOT NULL,
                provider_node_id TEXT NOT NULL,
                status TEXT NOT NULL,
                input_data_json TEXT NOT NULL,
                output_data_json TEXT,
                error_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                timeout_ms INTEGER NOT NULL DEFAULT 30000
            )
        """)
        # P5A-001 + Phase 8a-1 + ADR-007 + v0.23.0: Skills table columns
        # These are pre-migration-era columns — kept inline for backward compat with
        # databases created before the migration runner existed.
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(skills)").fetchall()}
        for col, col_type, default in [
            ("provider_public_key", "TEXT", None),
            ("announce_signature", "TEXT", None),
            ("provider_msg_id", "TEXT", None),
            ("sidecar_port", "INTEGER", "DEFAULT 0"),
            ("uri", "TEXT", "DEFAULT ''"),
            ("provider_host", "TEXT", "DEFAULT ''"),
            ("provider_port", "INTEGER", "DEFAULT 0"),
        ]:
            if col not in columns:
                ddl = f"ALTER TABLE skills ADD COLUMN {col} {col_type}"
                if default:
                    ddl += f" {default}"
                try:
                    cursor.execute(ddl)
                except sqlite3.OperationalError:
                    pass
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_skills_uri ON skills(uri)")

        # Phase 6a + Phase 7: Tasks table columns (pre-migration-era)
        task_columns = {row[1] for row in cursor.execute("PRAGMA table_info(tasks)").fetchall()}
        for col, col_type, default in [
            ("input_size_bytes", "INTEGER", None),
            ("wall_time_ms", "INTEGER", None),
            ("provider_public_key", "TEXT", "DEFAULT ''"),
        ]:
            if col not in task_columns:
                ddl = f"ALTER TABLE tasks ADD COLUMN {col} {col_type}"
                if default:
                    ddl += f" {default}"
                try:
                    cursor.execute(ddl)
                except sqlite3.OperationalError:
                    pass

        # Phase 6b + E-1: Peers table columns (pre-migration-era)
        peer_columns = {row[1] for row in cursor.execute("PRAGMA table_info(peers)").fetchall()}
        for col, col_type, default in [
            ("load", "INTEGER", "DEFAULT -1"),
            ("wallet", "TEXT", "DEFAULT ''"),
        ]:
            if col not in peer_columns:
                try:
                    cursor.execute(f"ALTER TABLE peers ADD COLUMN {col} {col_type} {default}")
                except sqlite3.OperationalError:
                    pass

        # Ledger table (Phase 5b)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                peer_public_key TEXT PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0.0,
                tasks_provided INTEGER NOT NULL DEFAULT 0,
                tasks_consumed INTEGER NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL,
                last_updated REAL NOT NULL
            )
        """)
        # Demand table (Phase 5b)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS demand (
                query_value TEXT PRIMARY KEY,
                query_type TEXT NOT NULL,
                query_count INTEGER NOT NULL DEFAULT 1,
                first_queried REAL NOT NULL,
                last_queried REAL NOT NULL
            )
        """)

        # Mail bucket tables (v0.29.1) — fresh nodes create buckets directly
        # Existing nodes get buckets + data migration via v0_29_1.sql
        _bucket_ddl = """
            CREATE TABLE IF NOT EXISTS {table} (
                rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id   TEXT UNIQUE NOT NULL,
                from_node    TEXT NOT NULL,
                to_node      TEXT NOT NULL,
                timestamp    REAL NOT NULL,
                body         TEXT NOT NULL,
                session_id   TEXT,
                msg_type     TEXT DEFAULT 'text',
                reply_to     TEXT,
                ttl_expires  REAL NOT NULL,
                status       TEXT DEFAULT 'unread',
                created_at   REAL NOT NULL DEFAULT 0,
                system       INTEGER DEFAULT 0,
                item_origin  TEXT DEFAULT 'skill'
            )
        """
        for bucket in ("mail_inbox", "mail_jobreport", "mail_system"):
            cursor.execute(_bucket_ddl.format(table=bucket))
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{bucket}_status ON {bucket}(status)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{bucket}_expires ON {bucket}(ttl_expires)")
        # Extra indexes for inbox (poll queries)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_inbox_session ON mail_inbox(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_inbox_from ON mail_inbox(from_node)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_inbox_type ON mail_inbox(msg_type)")

        # v0.32.0: Credit note bucket — fresh nodes create directly; existing nodes via migration
        cursor.execute(_bucket_ddl.format(table="mail_creditnote"))
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_creditnote_status ON mail_creditnote(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_creditnote_expires ON mail_creditnote(ttl_expires)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_creditnote_from ON mail_creditnote(from_node)")
        # session_id used as reference (job_id) for credit note lookup
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mail_creditnote_ref ON mail_creditnote(session_id)")

        # Legacy compat: keep 'mail' table if it exists (pre-v0.29.1 nodes)
        # The migration renames it to mail_legacy. Fresh nodes never create it.

        # Outbox (sender-side)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mail_outbox (
                item_id      TEXT PRIMARY KEY,
                to_node      TEXT NOT NULL,
                batch_seq    INTEGER NOT NULL,
                body_json    TEXT NOT NULL,
                status       TEXT DEFAULT 'pending',
                created_at   REAL NOT NULL,
                delivered_at REAL,
                ttl_expires  REAL NOT NULL,
                retry_count  INTEGER DEFAULT 0,
                last_attempt REAL DEFAULT 0
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_outbox_to ON mail_outbox(to_node, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_outbox_expires ON mail_outbox(ttl_expires)")

        # Sequence counters (per-recipient)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mail_seq (
                peer_node_id TEXT PRIMARY KEY,
                next_seq     INTEGER DEFAULT 1
            )
        """)

        # Address book
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS address_book (
                node_id      TEXT NOT NULL,
                tier         TEXT NOT NULL,
                label        TEXT,
                last_ip      TEXT,
                last_port    INTEGER,
                sidecar_port INTEGER DEFAULT 0,
                group_id     TEXT,
                last_seen    REAL,
                created_at   REAL NOT NULL,
                PRIMARY KEY (node_id, tier)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_addr_tier ON address_book(tier)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_addr_group ON address_book(group_id)")

        # Execution log (v0.13.0)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                caller_node_id TEXT,
                status TEXT,
                wall_time_ms INTEGER,
                input_hash TEXT,
                asset_hash TEXT,
                error TEXT,
                created_at REAL,
                quality_rating INTEGER
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_execlog_job ON execution_log(job_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_execlog_skill ON execution_log(skill_name)")
        # #16: Ensure quality_rating column exists on DBs created before it was added
        execlog_cols = {row[1] for row in cursor.execute("PRAGMA table_info(execution_log)").fetchall()}
        if "quality_rating" not in execlog_cols:
            cursor.execute("ALTER TABLE execution_log ADD COLUMN quality_rating INTEGER")

        # Async jobs (v0.13.0)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS async_jobs (
                job_id TEXT PRIMARY KEY,
                skill_name TEXT NOT NULL,
                consumer_node_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                status TEXT DEFAULT 'queued',
                queue_position INTEGER DEFAULT 0,
                result_json TEXT,
                error_json TEXT,
                created_at REAL,
                updated_at REAL,
                expires_at REAL,
                provider_node_id TEXT,
                provider_host TEXT,
                provider_port INTEGER
            )
        """)
        
        self._get_conn().commit()

        # v0.23.0+: Run versioned SQL migrations (replaces inline ALTER TABLEs)
        import os
        from ..core.migrations import run_migrations
        migrations_dir = os.path.join(os.path.dirname(__file__), '..', 'migrations')
        applied = run_migrations(self._get_conn(), migrations_dir)
        if applied:
            logger.info(f"Applied {applied} schema migration(s)")

    def _get_conn(self):
        return self._keepalive_conn

    @staticmethod
    def _mail_bucket(msg_type: str, system: bool) -> str:
        """Route mail to the correct bucket table based on msg_type and system flag."""
        if msg_type and msg_type.startswith("knarr/system/task_result"):
            return "mail_jobreport"
        if msg_type and msg_type.startswith("knarr/commerce/credit_note"):
            return "mail_creditnote"
        if system:
            return "mail_system"
        return "mail_inbox"

    def get_node_key(self) -> Optional[bytes]:
        conn = self._get_conn()
        cursor = conn.execute("SELECT key_bytes FROM node_identity")
        row = cursor.fetchone()
        return row[0] if row else None

    def set_node_key(self, key_bytes: bytes):
        conn = self._get_conn()
        conn.execute("INSERT INTO node_identity (key_bytes) VALUES (?)", (key_bytes,))
        conn.commit()

    def upsert_peer(self, node: NodeInfo):
        conn = self._get_conn()
        # SA-04: Cap peer table size
        existing = conn.execute("SELECT 1 FROM peers WHERE node_id = ?", (node.node_id,)).fetchone()
        if not existing:
            count = conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
            if count >= MAX_PEERS:
                # Evict oldest
                conn.execute("""
                    DELETE FROM peers WHERE node_id = (
                        SELECT node_id FROM peers ORDER BY last_seen ASC LIMIT 1
                    )
                """)
        
        conn.execute("""
            INSERT INTO peers (node_id, host, port, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                host=excluded.host,
                port=excluded.port,
                last_seen=excluded.last_seen
        """, (node.node_id, node.host, node.port, time.time()))
        conn.commit()

    def touch_peer(self, node_id: str):
        conn = self._get_conn()
        conn.execute("""
            UPDATE peers SET last_seen = ? WHERE node_id = ?
        """, (time.time(), node_id))
        conn.commit()

    def touch_peers(self, node_ids: list):
        """Batch-update last_seen for multiple peers in one transaction."""
        if not node_ids:
            return
        conn = self._get_conn()
        now = time.time()
        conn.executemany(
            "UPDATE peers SET last_seen = ? WHERE node_id = ?",
            [(now, nid) for nid in node_ids],
        )
        conn.commit()

    def get_peers(self) -> List[NodeInfo]:
        conn = self._get_conn()
        cursor = conn.execute("SELECT node_id, host, port FROM peers")
        return [NodeInfo(node_id=row[0], host=row[1], port=row[2]) for row in cursor.fetchall()]

    def get_cached_peers(self, max_age_hours: float = 24, limit: int = 10) -> List[NodeInfo]:
        """Returns peers seen within max_age_hours, ordered by most recent first."""
        conn = self._get_conn()
        cutoff = time.time() - (max_age_hours * 3600)
        cursor = conn.execute(
            "SELECT node_id, host, port FROM peers WHERE last_seen > ? ORDER BY last_seen DESC LIMIT ?",
            (cutoff, limit),
        )
        return [NodeInfo(node_id=r[0], host=r[1], port=r[2]) for r in cursor.fetchall()]

    def purge_stale_peers(self, max_age_hours: float = 72) -> int:
        """Delete peers not seen within max_age_hours. Returns count deleted."""
        conn = self._get_conn()
        cutoff = time.time() - (max_age_hours * 3600)
        cursor = conn.execute("DELETE FROM peers WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount

    def get_peers_full(self) -> List[Dict[str, Any]]:
        """Returns all peers with last_seen data."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT node_id, host, port, last_seen, load, wallet, encryption_key, jurisdiction FROM peers")
        return [
            {"node_id": r[0], "host": r[1], "port": r[2], "last_seen": r[3],
             "load": r[4], "wallet": r[5] or "", "encryption_key": r[6] or "",
             "jurisdiction": r[7] or ""}
            for r in cursor.fetchall()
        ]

    def update_peer_load(self, node_id: str, load: int):
        """Updates the load value for a peer."""
        conn = self._get_conn()
        conn.execute("UPDATE peers SET load = ? WHERE node_id = ?", (load, node_id))
        conn.commit()

    def update_peer_wallet(self, node_id: str, wallet: str):
        """Updates the wallet address for a peer."""
        conn = self._get_conn()
        conn.execute("UPDATE peers SET wallet = ? WHERE node_id = ?", (wallet, node_id))
        conn.commit()

    def update_peer_jurisdiction(self, node_id: str, jurisdiction: str):
        """Updates the jurisdiction for a peer."""
        conn = self._get_conn()
        conn.execute("UPDATE peers SET jurisdiction = ? WHERE node_id = ?", (jurisdiction, node_id))
        conn.commit()

    def remove_peer(self, node_id: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM peers WHERE node_id = ?", (node_id,))
        conn.commit()

    def upsert_skill(self, skill_key: str, provider_node_id: str, skill_sheet: SkillSheet,
                     ttl: int = 600, is_own: bool = False,
                     provider_public_key: Optional[str] = None,
                     announce_signature: Optional[str] = None,
                     provider_msg_id: Optional[str] = None,
                     sidecar_port: int = 0,
                     provider_host: str = "",
                     provider_port: int = 0):
        conn = self._get_conn()
        # SA-04: Cap skill table size
        existing = conn.execute(
            "SELECT 1 FROM skills WHERE skill_key = ? AND provider_node_id = ?",
            (skill_key, provider_node_id)
        ).fetchone()
        if not existing:
            count = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            if count >= MAX_SKILLS:
                # Evict oldest (approximate via announced_at)
                conn.execute("""
                    DELETE FROM skills WHERE rowid = (
                        SELECT rowid FROM skills ORDER BY announced_at ASC LIMIT 1
                    )
                """)

        uri = skill_sheet.uri or ""
        conn.execute("""
            INSERT INTO skills (skill_key, provider_node_id, skill_record_json, announced_at, ttl, is_own,
                               provider_public_key, announce_signature, provider_msg_id, sidecar_port, uri,
                               provider_host, provider_port)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_key, provider_node_id) DO UPDATE SET
                skill_record_json=excluded.skill_record_json,
                announced_at=excluded.announced_at,
                ttl=excluded.ttl,
                is_own=excluded.is_own,
                provider_public_key=excluded.provider_public_key,
                announce_signature=excluded.announce_signature,
                provider_msg_id=excluded.provider_msg_id,
                sidecar_port=excluded.sidecar_port,
                uri=excluded.uri,
                provider_host=excluded.provider_host,
                provider_port=excluded.provider_port
        """, (skill_key, provider_node_id, json.dumps(skill_sheet.to_dict()),
              time.time(), ttl, 1 if is_own else 0,
              provider_public_key, announce_signature, provider_msg_id, sidecar_port, uri,
              provider_host, provider_port))
        conn.commit()

    def get_own_skills(self) -> List[SkillSheet]:
        conn = self._get_conn()
        cursor = conn.execute("SELECT skill_record_json FROM skills WHERE is_own = 1")
        return [SkillSheet.from_dict(json.loads(row[0])) for row in cursor.fetchall()]

    def get_all_skills(self, since: float = 0.0) -> List[Dict[str, Any]]:
        """Returns all non-own skills announced after `since`, with signatures."""
        conn = self._get_conn()
        now = time.time()
        cursor = conn.execute("""
            SELECT skill_key, provider_node_id, skill_record_json, announced_at, ttl,
                   provider_public_key, announce_signature, provider_msg_id, sidecar_port
            FROM skills
            WHERE announced_at > ? AND (announced_at + ttl) > ?
            ORDER BY announced_at ASC
        """, (since, now))
        results = []
        for row in cursor.fetchall():
            results.append({
                "skill_key": row[0],
                "provider_node_id": row[1],
                "skill_sheet": json.loads(row[2]),
                "announced_at": row[3],
                "ttl": row[4],
                "public_key": row[5],
                "signature": row[6],
                "msg_id": row[7],
                "sidecar_port": row[8] or 0
            })
        return results

    def get_provider_address(self, node_id: str) -> Optional[tuple]:
        """Look up a provider's host/port from the skills table (gossip-discovered address)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT provider_host, provider_port FROM skills WHERE provider_node_id = ? AND provider_host != '' LIMIT 1",
            (node_id,)
        ).fetchone()
        if row and row[0] and row[1]:
            return (row[0], row[1])
        return None

    def remove_skill(self, skill_key: str, provider_node_id: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM skills WHERE skill_key = ? AND provider_node_id = ?", (skill_key, provider_node_id))
        conn.commit()

    def query_all_active_skills(self, peer_timeout: float = 300) -> List[Dict[str, Any]]:
        """Returns all active (non-expired) skills — both remote and own.
        Uses LEFT JOIN so gossip-discovered providers (not in peer table) are still visible.
        Falls back to skill-table provider_host/provider_port when peer table has no entry."""
        now = time.time()
        conn = self._get_conn()
        # Remote skills
        cursor = conn.execute("""
            SELECT s.provider_node_id, p.host, p.port, s.skill_record_json,
                   p.last_seen, s.announced_at, p.load, s.provider_public_key, s.sidecar_port,
                   s.provider_host, s.provider_port
            FROM skills s
            LEFT JOIN peers p ON s.provider_node_id = p.node_id
            WHERE (s.announced_at + s.ttl) > ?
              AND s.is_own = 0
              AND (p.last_seen > ? OR (p.last_seen IS NULL AND s.provider_host != ''))
        """, (now, now - peer_timeout))
        results = []
        for row in cursor.fetchall():
            # Prefer peer table address, fall back to skill-embedded address
            host = row[1] or row[9] or ""
            port = row[2] or row[10] or 0
            if not host or not port:
                continue  # No reachable address at all
            results.append({
                "node_id": row[0],
                "host": host,
                "port": port,
                "skill_sheet": json.loads(row[3]),
                "_last_seen": row[4],
                "_announced_at": row[5],
                "_load": row[6],
                "_provider_public_key": row[7] or "",
                "sidecar_port": row[8] or 0,
                "is_local": False,
            })
        # BUG-26: Include own skills in network listing
        own_cursor = conn.execute("""
            SELECT s.provider_node_id, s.skill_record_json, s.announced_at,
                   s.provider_public_key, s.sidecar_port,
                   s.provider_host, s.provider_port
            FROM skills s
            WHERE s.is_own = 1
        """)
        for row in own_cursor.fetchall():
            results.append({
                "node_id": row[0],
                "host": row[5] or "127.0.0.1",
                "port": row[6] or 0,
                "skill_sheet": json.loads(row[1]),
                "_last_seen": now,
                "_announced_at": row[2],
                "_load": 0,
                "_provider_public_key": row[3] or "",
                "sidecar_port": row[4] or 0,
                "is_local": True,
            })
        return results

    def query_skills_by_name(self, name: str, peer_timeout: float = 300) -> List[Dict[str, Any]]:
        now = time.time()
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT s.provider_node_id, p.host, p.port, s.skill_record_json,
                   p.last_seen, s.announced_at, p.load, s.provider_public_key, s.sidecar_port,
                   s.provider_host, s.provider_port
            FROM skills s
            LEFT JOIN peers p ON s.provider_node_id = p.node_id
            WHERE s.skill_key = ?
              AND (s.announced_at + s.ttl) > ?
              AND s.is_own = 0
              AND (p.last_seen > ? OR (p.last_seen IS NULL AND s.provider_host != ''))
        """, (name, now, now - peer_timeout))
        results = []
        for row in cursor.fetchall():
            host = row[1] or row[9] or ""
            port = row[2] or row[10] or 0
            if not host or not port:
                continue
            results.append({
                "node_id": row[0],
                "host": host,
                "port": port,
                "skill_sheet": json.loads(row[3]),
                "_last_seen": row[4],
                "_announced_at": row[5],
                "_load": row[6],
                "_provider_public_key": row[7] or "",
                "sidecar_port": row[8] or 0,
            })
        return results

    def query_skills_by_tag(self, tag: str, peer_timeout: float = 300) -> List[Dict[str, Any]]:
        now = time.time()
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT s.provider_node_id, p.host, p.port, s.skill_record_json,
                   p.last_seen, s.announced_at, p.load, s.provider_public_key, s.sidecar_port,
                   s.provider_host, s.provider_port
            FROM skills s
            LEFT JOIN peers p ON s.provider_node_id = p.node_id
            WHERE (s.announced_at + s.ttl) > ?
              AND s.is_own = 0
              AND (p.last_seen > ? OR (p.last_seen IS NULL AND s.provider_host != ''))
        """, (now, now - peer_timeout))
        results = []
        for row in cursor.fetchall():
            skill_sheet_dict = json.loads(row[3])
            tags = [t.lower() for t in skill_sheet_dict.get("tags", [])]
            if tag.lower() in tags:
                host = row[1] or row[9] or ""
                port = row[2] or row[10] or 0
                if not host or not port:
                    continue
                results.append({
                    "node_id": row[0],
                    "host": host,
                    "port": port,
                    "skill_sheet": skill_sheet_dict,
                    "_last_seen": row[4],
                    "_announced_at": row[5],
                    "_load": row[6],
                    "_provider_public_key": row[7] or "",
                    "sidecar_port": row[8] or 0,
                })
        return results

    def _skills_from_rows(self, rows) -> List[Dict[str, Any]]:
        """Convert raw skill query rows to result dicts."""
        results = []
        for row in rows:
            results.append({
                "node_id": row[0],
                "host": row[1],
                "port": row[2],
                "skill_sheet": json.loads(row[3]),
                "_last_seen": row[4],
                "_announced_at": row[5] if len(row) > 5 else 0,
                "_load": row[6] if len(row) > 6 else -1,
                "_provider_public_key": (row[7] or "") if len(row) > 7 else "",
                "sidecar_port": (row[8] or 0) if len(row) > 8 else 0,
            })
        return results

    def query_skills_by_uri(self, uri: str, peer_timeout: float = 300) -> List[Dict[str, Any]]:
        """Exact URI match — returns all providers for this URI."""
        now = time.time()
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT s.provider_node_id, p.host, p.port, s.skill_record_json,
                   p.last_seen, s.announced_at, p.load, s.provider_public_key, s.sidecar_port
            FROM skills s JOIN peers p ON s.provider_node_id = p.node_id
            WHERE s.uri = ? AND (s.announced_at + s.ttl) > ? AND p.last_seen > ? AND s.is_own = 0
        """, (uri, now, now - peer_timeout))
        return self._skills_from_rows(cursor.fetchall())

    def query_skills_by_uri_prefix(self, prefix: str, peer_timeout: float = 300) -> List[Dict[str, Any]]:
        """URI prefix browse — 'knarr:///audio/' returns all audio skills."""
        now = time.time()
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT s.provider_node_id, p.host, p.port, s.skill_record_json,
                   p.last_seen, s.announced_at, p.load, s.provider_public_key, s.sidecar_port
            FROM skills s JOIN peers p ON s.provider_node_id = p.node_id
            WHERE s.uri LIKE ? AND (s.announced_at + s.ttl) > ? AND p.last_seen > ? AND s.is_own = 0
        """, (prefix + '%', now, now - peer_timeout))
        return self._skills_from_rows(cursor.fetchall())

    def prune_stale_skills(self) -> int:
        now = time.time()
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM skills WHERE (announced_at + ttl) < ? AND is_own = 0", (now,))
        conn.commit()
        return cur.rowcount

    def prune_stale_peers(self, timeout: float = 300, exclude_node_id: str = "") -> int:
        """Remove peers not seen within timeout. Never prune our own node_id (S-026)."""
        now = time.time()
        conn = self._get_conn()
        if exclude_node_id:
            cur = conn.execute(
                "DELETE FROM peers WHERE last_seen < ? AND node_id != ?",
                (now - timeout, exclude_node_id),
            )
        else:
            cur = conn.execute("DELETE FROM peers WHERE last_seen < ?", (now - timeout,))
        conn.commit()
        return cur.rowcount

    def insert_task(self, task: Task, provider_public_key: str = ""):
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO tasks (
                task_id, skill_name, requester_node_id, provider_node_id, status,
                input_data_json, created_at, updated_at, timeout_ms, provider_public_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.task_id, task.skill_name, task.requester_node_id, task.provider_node_id,
            task.status, json.dumps(task.input_data), task.created_at, task.updated_at,
            task.timeout_ms, provider_public_key
        ))
        conn.commit()

    def log_execution(self, job_id: str, skill: str, caller: Optional[str],
                      status: str, wall_ms: int, input_hash: Optional[str] = None,
                      asset_hash: Optional[str] = None, error: Optional[str] = None,
                      price: Optional[float] = None, price_breakdown: str = ""):
        """Records an execution event to the append-only log."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO execution_log (
                job_id, skill_name, caller_node_id, status, wall_time_ms,
                input_hash, asset_hash, error, created_at, price, price_breakdown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, skill, caller, status, wall_ms, input_hash, asset_hash, error, time.time(), price, price_breakdown))
        conn.commit()

    def get_execution_log(self, job_id: Optional[str] = None, skill: Optional[str] = None,
                          limit: int = 100) -> List[Dict[str, Any]]:
        """Queries the execution log."""
        conn = self._get_conn()
        query = "SELECT job_id, skill_name, caller_node_id, status, wall_time_ms, error, created_at FROM execution_log"
        params = []
        if job_id or skill:
            query += " WHERE"
            if job_id:
                query += " job_id = ?"
                params.append(job_id)
            if skill:
                if job_id: query += " AND"
                query += " skill_name = ?"
                params.append(skill)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(query, tuple(params))
        return [
            {"job_id": r[0], "skill": r[1], "caller": r[2], "status": r[3],
             "wall_time_ms": r[4], "error": r[5], "created_at": r[6]}
            for r in cursor.fetchall()
        ]

    def insert_async_job(self, job_id: str, skill: str, consumer_id: str,
                         input_hash: str, position: int, expires_at: float):
        """Inserts a new async job record."""
        conn = self._get_conn()
        now = time.time()
        conn.execute("""
            INSERT INTO async_jobs (
                job_id, skill_name, consumer_node_id, input_hash,
                status, queue_position, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
        """, (job_id, skill, consumer_id, input_hash, position, now, now, expires_at))
        conn.commit()

    def insert_remote_job(self, job_id: str, skill: str, provider_node_id: str,
                          provider_host: str, provider_port: int, expires_at: float) -> bool:
        """Insert a tracking entry for a remote async job. Returns False on PK collision."""
        conn = self._get_conn()
        now = time.time()
        try:
            conn.execute("""
                INSERT INTO async_jobs (
                    job_id, skill_name, consumer_node_id, input_hash,
                    status, queue_position, created_at, updated_at, expires_at,
                    provider_node_id, provider_host, provider_port
                ) VALUES (?, ?, ?, '', 'remote', 0, ?, ?, ?, ?, ?, ?)
            """, (job_id, skill, "", now, now, expires_at,
                  provider_node_id, provider_host, provider_port))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_async_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves an async job by ID."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT job_id, skill_name, consumer_node_id, input_hash,
                   status, queue_position, result_json, error_json,
                   created_at, updated_at, expires_at,
                   provider_node_id, provider_host, provider_port
            FROM async_jobs WHERE job_id = ?
        """, (job_id,))
        r = cursor.fetchone()
        if not r: return None
        return {
            "job_id": r[0], "skill": r[1], "consumer_id": r[2], "input_hash": r[3],
            "status": r[4], "position": r[5],
            "result": json.loads(r[6]) if r[6] else None,
            "error": json.loads(r[7]) if r[7] else None,
            "created_at": r[8], "updated_at": r[9], "expires_at": r[10],
            "provider_node_id": r[11], "provider_host": r[12], "provider_port": r[13]
        }

    def get_async_job_by_hash(self, input_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieves an async job by its dedup hash."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT job_id, skill_name, consumer_node_id, input_hash,
                   status, queue_position, created_at, updated_at, expires_at
            FROM async_jobs WHERE input_hash = ? AND status IN ('accepted', 'running', 'queued')
        """, (input_hash,))
        r = cursor.fetchone()
        if not r: return None
        return {
            "job_id": r[0], "skill": r[1], "consumer_id": r[2], "input_hash": r[3],
            "status": r[4], "position": r[5],
            "created_at": r[6], "updated_at": r[7], "expires_at": r[8]
        }

    def update_async_job_status(self, job_id: str, status: str,
                                 result: Optional[Dict[str, Any]] = None,
                                 error: Optional[Dict[str, Any]] = None,
                                 position: Optional[int] = None):
        """Updates the status and result/error of an async job."""
        conn = self._get_conn()
        now = time.time()
        fields = ["status = ?", "updated_at = ?"]
        params = [status, now]
        if result is not None:
            fields.append("result_json = ?")
            params.append(json.dumps(result))
        if error is not None:
            fields.append("error_json = ?")
            params.append(json.dumps(error))
        if position is not None:
            fields.append("queue_position = ?")
            params.append(position)
        
        params.append(job_id)
        conn.execute(f"UPDATE async_jobs SET {', '.join(fields)} WHERE job_id = ?", tuple(params))
        conn.commit()

    def cleanup_expired_jobs(self):
        """Removes expired jobs from the database."""
        conn = self._get_conn()
        now = time.time()
        conn.execute("UPDATE async_jobs SET status = 'expired' WHERE expires_at < ? AND status != 'expired'", (now,))
        conn.commit()

    def delete_old_expired_jobs(self, days: int = 7):
        """Actually deletes very old expired jobs."""
        conn = self._get_conn()
        cutoff = time.time() - (days * 86400)
        conn.execute("DELETE FROM async_jobs WHERE status = 'expired' AND updated_at < ?", (cutoff,))
        conn.commit()

    def update_task_status(self, task_id: str, status: str,
                           output_data: Optional[Dict[str, Any]] = None,
                           error: Optional[Dict[str, Any]] = None,
                           input_size_bytes: Optional[int] = None,
                           wall_time_ms: Optional[int] = None,
                           provider_public_key: Optional[str] = None):
        conn = self._get_conn()
        now = time.time()
        
        # Build dynamic query to only update provided fields
        fields = ["status = ?", "updated_at = ?"]
        params = [status, now]
        
        if output_data is not None:
            fields.append("output_data_json = ?")
            params.append(json.dumps(output_data))
        if error is not None:
            fields.append("error_json = ?")
            params.append(json.dumps(error))
        if input_size_bytes is not None:
            fields.append("input_size_bytes = ?")
            params.append(input_size_bytes)
        if wall_time_ms is not None:
            fields.append("wall_time_ms = ?")
            params.append(wall_time_ms)
        if provider_public_key is not None:
            fields.append("provider_public_key = ?")
            params.append(provider_public_key)
            
        params.append(task_id)
        query = f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?"
        conn.execute(query, params)
        conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT task_id, skill_name, requester_node_id, provider_node_id, status,
                   input_data_json, output_data_json, error_json, created_at, updated_at, timeout_ms
            FROM tasks WHERE task_id = ?
        """, (task_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return Task(
            task_id=row[0],
            skill_name=row[1],
            requester_node_id=row[2],
            provider_node_id=row[3],
            status=row[4],
            input_data=json.loads(row[5]),
            output_data=json.loads(row[6]) if row[6] is not None else None,
            error=json.loads(row[7]) if row[7] is not None else None,
            created_at=row[8],
            updated_at=row[9],
            timeout_ms=row[10]
        )

    def get_skill_task_stats(self, skill_name: str) -> Dict[str, Any]:
        """Returns aggregated telemetry for a skill. Feeds RETRY_AFTER and cockpit."""
        conn = self._get_conn()
        
        # Aggregate stats
        cursor = conn.execute("""
            SELECT COUNT(*), AVG(wall_time_ms), AVG(input_size_bytes),
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
            FROM tasks
            WHERE skill_name = ? AND wall_time_ms IS NOT NULL
        """, (skill_name,))
        row = cursor.fetchone()
        total = row[0] or 0
        completed = row[3] or 0

        # Percentiles (fetch all wall times for completed tasks)
        cursor2 = conn.execute("""
            SELECT wall_time_ms FROM tasks
            WHERE skill_name = ? AND status = 'completed' AND wall_time_ms IS NOT NULL
            ORDER BY wall_time_ms
        """, (skill_name,))
        wall_times = [r[0] for r in cursor2.fetchall()]

        p50 = wall_times[len(wall_times) // 2] if wall_times else 0
        p95_idx = min(int(len(wall_times) * 0.95), len(wall_times) - 1) if wall_times else 0
        p95 = wall_times[p95_idx] if wall_times else 0

        success_rate = (completed / total) if total > 0 else 0.0

        return {
            "total_completed": completed,
            "total_tasks": total,
            "avg_wall_time_ms": row[1] or 0.0,
            "avg_input_bytes": row[2] or 0.0,
            "p50_ms": p50,
            "p95_ms": p95,
            "success_rate": success_rate,
        }

    def get_recent_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns most recent tasks with telemetry. Feeds cockpit task feed."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT task_id, skill_name, status, input_size_bytes, wall_time_ms,
                   created_at, updated_at, requester_node_id, provider_node_id
            FROM tasks ORDER BY created_at DESC LIMIT ?
        """, (limit,))
        return [
            {"task_id": r[0], "skill_name": r[1], "status": r[2],
             "input_size_bytes": r[3], "wall_time_ms": r[4],
             "created_at": r[5], "updated_at": r[6],
             "requester_node_id": r[7], "provider_node_id": r[8]}
            for r in cursor.fetchall()
        ]

    def get_provider_reputation(self, provider_node_id: str,
                                skill_name: str = None,
                                window_days: int = 30) -> Dict[str, Any]:
        """Aggregates task outcomes for a provider from this consumer's perspective.
        Returns raw data. Does NOT compute scores or make policy decisions."""
        conn = self._get_conn()
        cutoff = time.time() - (window_days * 86400)

        if skill_name:
            cursor = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                       AVG(CASE WHEN status = 'completed' THEN wall_time_ms END) as avg_wall_ms,
                       MAX(updated_at) as last_interaction
                FROM tasks
                WHERE provider_node_id = ?
                  AND skill_name = ?
                  AND updated_at > ?
                  AND status IN ('completed', 'failed')
            """, (provider_node_id, skill_name, cutoff))
        else:
            cursor = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                       AVG(CASE WHEN status = 'completed' THEN wall_time_ms END) as avg_wall_ms,
                       MAX(updated_at) as last_interaction
                FROM tasks
                WHERE provider_node_id = ?
                  AND updated_at > ?
                  AND status IN ('completed', 'failed')
            """, (provider_node_id, cutoff))

        row = cursor.fetchone()
        total = row[0] or 0
        completed = row[1] or 0
        failed = row[2] or 0

        return {
            "total_tasks": total,
            "completed": completed,
            "failed": failed,
            "success_rate": (completed / total) if total > 0 else None,
            "avg_wall_time_ms": row[3],
            "last_interaction": row[4],
        }

    def get_counterparty_count(self) -> int:
        """Returns number of unique peers in the ledger."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT COUNT(*) FROM ledger")
        return cursor.fetchone()[0]

    def get_all_provider_reputations(self, window_days: int = 30) -> List[Dict[str, Any]]:
        """Bulk reputation query for all providers with task history."""
        conn = self._get_conn()
        cutoff = time.time() - (window_days * 86400)
        cursor = conn.execute("""
            SELECT provider_node_id,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                   AVG(CASE WHEN status = 'completed' THEN wall_time_ms END) as avg_wall_ms,
                   MAX(updated_at) as last_interaction
            FROM tasks
            WHERE updated_at > ?
              AND status IN ('completed', 'failed')
            GROUP BY provider_node_id
        """, (cutoff,))
        results = []
        for row in cursor.fetchall():
            total = row[1] or 0
            completed = row[2] or 0
            results.append({
                "provider_node_id": row[0],
                "total_tasks": total,
                "completed": completed,
                "failed": row[3] or 0,
                "success_rate": (completed / total) if total > 0 else None,
                "avg_wall_time_ms": row[4],
                "last_interaction": row[5],
            })
        return results

    def prune_completed_tasks(self, max_age: float = 300):
        now = time.time()
        conn = self._get_conn()
        conn.execute("""
            DELETE FROM tasks 
            WHERE status IN ('completed', 'failed', 'rejected') 
              AND updated_at < ?
        """, (now - max_age,))
        conn.commit()

    # Ledger methods
    def get_or_create_ledger_entry(self, peer_public_key: str, initial_balance: float = 0.0, initial_trust: float = 0.3) -> LedgerEntry:
        """Gets or creates a ledger entry. New entries get initial_balance and initial_trust."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT peer_public_key, balance, tasks_provided, tasks_consumed, first_seen, last_updated "
            "FROM ledger WHERE peer_public_key = ?", (peer_public_key,)
        )
        row = cursor.fetchone()
        if row:
            return LedgerEntry(
                peer_public_key=row[0], balance=row[1],
                tasks_provided=row[2], tasks_consumed=row[3],
                first_seen=row[4], last_updated=row[5]
            )
        # Create new entry
        now = time.time()
        # Cap ledger size
        count = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        if count >= MAX_LEDGER_ENTRIES:
            # Evict oldest
            conn.execute("""
                DELETE FROM ledger WHERE peer_public_key = (
                    SELECT peer_public_key FROM ledger ORDER BY last_updated ASC LIMIT 1
                )
            """)
        conn.execute(
            "INSERT INTO ledger (peer_public_key, balance, tasks_provided, tasks_consumed, first_seen, last_updated, trust) "
            "VALUES (?, ?, 0, 0, ?, ?, ?)",
            (peer_public_key, initial_balance, now, now, initial_trust)
        )
        conn.commit()
        return LedgerEntry(
            peer_public_key=peer_public_key, balance=initial_balance,
            tasks_provided=0, tasks_consumed=0, first_seen=now, last_updated=now
        )

    def update_ledger_provider(self, peer_public_key: str, price: float):
        """Provider side: consumer spent credit. Decrement their balance, increment tasks_provided."""
        # print(f"DEBUG: update_ledger_provider key={peer_public_key[:8]}... price={price}")
        conn = self._get_conn()
        now = time.time()
        cursor = conn.execute("""
            UPDATE ledger SET
                balance = balance - ?,
                tasks_provided = tasks_provided + 1,
                last_updated = ?
            WHERE peer_public_key = ?
        """, (price, now, peer_public_key))
        # print(f"DEBUG: updated {cursor.rowcount} rows")
        conn.commit()

    def update_ledger_consumer(self, peer_public_key: str, price: float):
        """Consumer side: provider earned credit. Increment their balance, increment tasks_consumed."""
        conn = self._get_conn()
        now = time.time()
        conn.execute("""
            UPDATE ledger SET
                balance = balance + ?,
                tasks_consumed = tasks_consumed + 1,
                last_updated = ?
            WHERE peer_public_key = ?
        """, (price, now, peer_public_key))
        conn.commit()

    def get_ledger_balance(self, peer_public_key: str) -> Optional[float]:
        """Returns the balance for a peer, or None if unknown."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT balance FROM ledger WHERE peer_public_key = ?", (peer_public_key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_all_ledger_entries(self) -> List[Dict[str, Any]]:
        """Returns all ledger entries. Feeds cockpit economy panel."""
        conn = self._get_conn()
        # B5: Include prepaid, pub_tab, soft_limit, hard_limit (v0.31.0 columns)
        # Use COALESCE for backward compat with pre-migration databases
        cursor = conn.execute("""
            SELECT peer_public_key, balance, tasks_provided, tasks_consumed,
                   first_seen, last_updated
            FROM ledger ORDER BY last_updated DESC
        """)
        # Detect if new columns exist
        col_names = [desc[0] for desc in cursor.description]
        results = []
        for r in cursor.fetchall():
            entry = {"peer_public_key": r[0], "balance": r[1], "tasks_provided": r[2],
                     "tasks_consumed": r[3], "first_seen": r[4], "last_updated": r[5]}
            results.append(entry)
        # Try fetching extended columns if migration has run
        try:
            ext_cursor = conn.execute("""
                SELECT peer_public_key, prepaid, pub_tab, soft_limit, hard_limit
                FROM ledger
            """)
            ext_map = {r[0]: {"prepaid": r[1], "pub_tab": r[2],
                              "soft_limit": r[3], "hard_limit": r[4]}
                       for r in ext_cursor.fetchall()}
            for entry in results:
                ext = ext_map.get(entry["peer_public_key"], {})
                entry["prepaid"] = ext.get("prepaid", 0.0)
                entry["pub_tab"] = ext.get("pub_tab", 0.0)
                entry["soft_limit"] = ext.get("soft_limit", 0.0)
                entry["hard_limit"] = ext.get("hard_limit", 0.0)
        except Exception:
            # Columns don't exist yet (pre-migration) — return defaults
            for entry in results:
                entry["prepaid"] = 0.0
                entry["pub_tab"] = 0.0
                entry["soft_limit"] = 0.0
                entry["hard_limit"] = 0.0
        return results

    def poll_task_results(self, limit: int = 20, status: str = "unread") -> list:
        """E4: Query mail_jobreport + mail_system, merged and sorted by timestamp."""
        conn = self._get_conn()
        limit = min(max(1, limit), 50)
        rows = []
        for table in ("mail_jobreport", "mail_system"):
            try:
                if status == "all":
                    cur = conn.execute(f"""
                        SELECT message_id, from_node, body, msg_type, status, created_at
                        FROM {table}
                        ORDER BY created_at DESC LIMIT ?
                    """, (limit,))
                else:
                    cur = conn.execute(f"""
                        SELECT message_id, from_node, body, msg_type, status, created_at
                        FROM {table}
                        WHERE status = ?
                        ORDER BY created_at DESC LIMIT ?
                    """, (status, limit))
                for r in cur.fetchall():
                    rows.append({
                        "message_id": r[0], "from_node": r[1], "body": r[2],
                        "msg_type": r[3], "status": r[4], "created_at": r[5],
                        "source": table,
                    })
            except Exception:
                continue  # Table may not exist on older schemas
        rows.sort(key=lambda r: r["created_at"] or 0, reverse=True)
        return rows[:limit]

    def decay_stale_balances(self, decay_rate: float, stale_seconds: float) -> int:
        """Decay balances of ledger entries not active within stale_seconds.

        Multiplies balance by (1 - decay_rate) for stale entries.
        Returns the number of entries decayed.
        """
        conn = self._get_conn()
        cutoff = time.time() - stale_seconds
        cursor = conn.execute("""
            UPDATE ledger SET balance = balance * ?
            WHERE last_updated < ? AND ABS(balance) > 0.01
        """, (1.0 - decay_rate, cutoff))
        conn.commit()
        return cursor.rowcount

    # Demand methods
    def record_demand(self, query_type: str, query_value: str):
        """Records a zero-result query as demand."""
        conn = self._get_conn()
        now = time.time()
        # Check if this is an update (existing entry) or insert (new entry)
        existing = conn.execute(
            "SELECT 1 FROM demand WHERE query_value = ?", (query_value,)
        ).fetchone()
        if not existing:
            count = conn.execute("SELECT COUNT(*) FROM demand").fetchone()[0]
            if count >= MAX_DEMAND_ENTRIES:
                # Evict least-queried entry
                conn.execute("""
                    DELETE FROM demand WHERE query_value = (
                        SELECT query_value FROM demand ORDER BY last_queried ASC LIMIT 1
                    )
                """)
        conn.execute("""
            INSERT INTO demand (query_value, query_type, query_count, first_queried, last_queried)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(query_value) DO UPDATE SET
                query_count = demand.query_count + 1,
                last_queried = ?
        """, (query_value, query_type, now, now, now))
        conn.commit()

    def get_demand(self) -> List[Dict[str, Any]]:
        """Returns demand records ordered by count descending."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT query_value, query_type, query_count, first_queried, last_queried
            FROM demand ORDER BY query_count DESC
        """)
        return [
            {"value": row[0], "type": row[1], "count": row[2],
             "first_queried": row[3], "last_queried": row[4]}
            for row in cursor.fetchall()
        ]

    # Mail methods (Phase 9a)

    def store_mail(self, message_id: str, from_node: str, to_node: str,
                   timestamp: float, body: str, session_id: Optional[str],
                   msg_type: str, reply_to: Optional[str], ttl_expires: float,
                   system: bool = False):
        """Store a new mail message, routing to the correct bucket table."""
        bucket = self._mail_bucket(msg_type, system)
        logger.debug(f"MAIL_BUCKET_ROUTE msg={message_id[:8]} bucket={bucket} type={msg_type} system={system}")
        conn = self._get_conn()
        conn.execute(f"""
            INSERT INTO {bucket} (message_id, from_node, to_node, timestamp, body,
                              session_id, msg_type, reply_to, ttl_expires, status, created_at, system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?)
        """, (message_id, from_node, to_node, timestamp, body,
              session_id, msg_type, reply_to, ttl_expires, timestamp, 1 if system else 0))
        conn.commit()

    def poll_mail(self, to_node: str, since_rowid: int = 0,
                  from_node: Optional[str] = None,
                  session_id: Optional[str] = None,
                  msg_type: Optional[str] = None,
                  status: str = "unread",
                  limit: int = 50,
                  system: Optional[int] = None,
                  bucket: str = "mail_inbox") -> tuple:
        """Poll mail messages from a bucket. Returns (rows, gap) where gap is True if since_rowid was purged."""
        if bucket not in MAIL_BUCKETS:
            bucket = "mail_inbox"
        conn = self._get_conn()
        now = time.time()
        gap = False

        conditions = ["to_node = ?", "ttl_expires > ?"]
        params: list = [to_node, now]

        if system is not None:
            conditions.append("system = ?")
            params.append(system)

        if since_rowid > 0:
            cursor = conn.execute(f"SELECT 1 FROM {bucket} WHERE rowid = ?", (since_rowid,))
            if cursor.fetchone() is None:
                oldest = conn.execute(f"SELECT MIN(rowid) FROM {bucket} WHERE to_node = ?", (to_node,)).fetchone()
                if oldest and oldest[0] is not None:
                    since_rowid = max(0, oldest[0] - 1)
                    gap = True
                else:
                    since_rowid = 0
            conditions.append("rowid > ?")
            params.append(since_rowid)

        if from_node:
            conditions.append("from_node = ?")
            params.append(from_node)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if msg_type:
            conditions.append("msg_type = ?")
            params.append(msg_type)
        if status and status != "all":
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = conn.execute(f"""
            SELECT rowid, message_id, from_node, to_node, timestamp, body,
                   session_id, msg_type, reply_to, ttl_expires, status
            FROM {bucket}
            WHERE {where}
            ORDER BY rowid ASC
            LIMIT ?
        """, params)

        rows = []
        for r in cursor.fetchall():
            rows.append({
                "rowid": r[0],
                "message_id": r[1],
                "from_node": r[2],
                "to_node": r[3],
                "timestamp": r[4],
                "body": r[5],
                "session_id": r[6],
                "msg_type": r[7],
                "reply_to": r[8],
                "ttl_expires": r[9],
                "status": r[10],
            })
        return rows, gap

    def ack_mail(self, message_ids: List[str], to_node: str, disposition: str) -> int:
        """Acknowledge mail messages across all bucket tables. Returns count affected."""
        if not message_ids:
            return 0
        conn = self._get_conn()
        count = 0
        placeholders = ",".join("?" for _ in message_ids)
        for bucket in MAIL_BUCKETS:
            if disposition == "deleted":
                cursor = conn.execute(
                    f"DELETE FROM {bucket} WHERE message_id IN ({placeholders}) AND to_node = ?",
                    (*message_ids, to_node)
                )
            else:
                cursor = conn.execute(
                    f"UPDATE {bucket} SET status = ? WHERE message_id IN ({placeholders}) AND to_node = ?",
                    (disposition, *message_ids, to_node)
                )
            count += cursor.rowcount
        conn.commit()
        return count

    def get_mail_message(self, message_id: str, to_node: str) -> Optional[dict]:
        """Get a single mail message by ID, searching across all bucket tables."""
        conn = self._get_conn()
        for bucket in ("mail_inbox", "mail_jobreport", "mail_system"):
            cursor = conn.execute(
                f"SELECT rowid, message_id, from_node, to_node, timestamp, body, "
                f"session_id, msg_type, reply_to, ttl_expires, status "
                f"FROM {bucket} WHERE message_id = ? AND to_node = ?",
                (message_id, to_node)
            )
            r = cursor.fetchone()
            if r is not None:
                return {
                    "rowid": r[0], "message_id": r[1], "from_node": r[2],
                    "to_node": r[3], "timestamp": r[4], "body": r[5],
                    "session_id": r[6], "msg_type": r[7], "reply_to": r[8],
                    "ttl_expires": r[9], "status": r[10],
                }
        return None

    def count_mail_inbox(self) -> int:
        """Count total messages in the inbox bucket."""
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM mail_inbox").fetchone()[0]

    # Backward-compat alias
    count_mail = count_mail_inbox

    def get_stale_inbox_messages(self, cutoff_timestamp: float, limit: int = 10) -> list:
        """v0.33.0: Get unread inbox messages older than cutoff for mail.inbox_stale event."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT message_id, from_node, timestamp FROM mail_inbox WHERE status = 'unread' AND timestamp < ? ORDER BY timestamp ASC LIMIT ?",
            (cutoff_timestamp, limit)
        ).fetchall()
        return [{"item_id": r[0], "from_node": r[1], "timestamp": r[2]} for r in rows]

    def count_mail_unread(self, to_node: str, system: int | None = None,
                          bucket: str = "mail_inbox") -> int:
        """Count unread messages for a node in a bucket."""
        if bucket not in MAIL_BUCKETS:
            bucket = "mail_inbox"
        conn = self._get_conn()
        sql = f"SELECT COUNT(*) FROM {bucket} WHERE to_node = ? AND status = 'unread'"
        params: list = [to_node]
        if system is not None:
            sql += " AND system = ?"
            params.append(system)
        return conn.execute(sql, params).fetchone()[0]

    def purge_expired_mail(self, now: float) -> int:
        """Delete expired mail messages from ALL bucket tables."""
        conn = self._get_conn()
        total = 0
        for bucket in MAIL_BUCKETS:
            cursor = conn.execute(f"DELETE FROM {bucket} WHERE ttl_expires < ?", (now,))
            total += cursor.rowcount
        conn.commit()
        return total

    def trim_bucket(self, bucket: str, max_rows: int) -> int:
        """Delete oldest rows when bucket exceeds max_rows. Returns count deleted."""
        if bucket not in MAIL_BUCKETS:
            return 0
        conn = self._get_conn()
        count = conn.execute(f"SELECT COUNT(*) FROM {bucket}").fetchone()[0]
        if count <= max_rows:
            return 0
        excess = count - max_rows
        cursor = conn.execute(
            f"DELETE FROM {bucket} WHERE rowid IN "
            f"(SELECT rowid FROM {bucket} ORDER BY rowid ASC LIMIT ?)",
            (excess,)
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info(f"BUCKET_TRIM bucket={bucket} had={count} max={max_rows} deleted={deleted}")
        return deleted

    def purge_bucket_by_age(self, bucket: str, max_age_seconds: float) -> int:
        """Delete rows older than max_age_seconds from a specific bucket."""
        if bucket not in MAIL_BUCKETS:
            return 0
        conn = self._get_conn()
        cutoff = time.time() - max_age_seconds
        cursor = conn.execute(f"DELETE FROM {bucket} WHERE created_at < ?", (cutoff,))
        conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info(f"BUCKET_AGE_PURGE bucket={bucket} ttl={max_age_seconds:.0f}s deleted={deleted}")
        return deleted

    def purge_execution_log_by_age(self, max_age_seconds: float) -> int:
        """Delete execution_log entries older than max_age_seconds."""
        conn = self._get_conn()
        cutoff = time.time() - max_age_seconds
        cursor = conn.execute("DELETE FROM execution_log WHERE created_at < ?", (cutoff,))
        conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info(f"EXECLOG_PURGE ttl={max_age_seconds:.0f}s deleted={deleted}")
        return deleted

    # Mail v2 Outbox Methods

    def enqueue_outbox(self, item_id: str, to_node: str, body_json: str, ttl_expires: float) -> int:
        """Stores item in outbox, returns its batch_seq."""
        # Validate to_node is a 64-char hex string (#21)
        if not to_node or len(to_node) != 64 or not all(c in '0123456789abcdef' for c in to_node):
            raise ValueError(f"Invalid to_node: must be 64-char hex, got '{to_node[:20]}'")
        conn = self._get_conn()
        now = time.time()
        
        # Get next sequence for this peer
        cursor = conn.execute("SELECT next_seq FROM mail_seq WHERE peer_node_id = ?", (to_node,))
        row = cursor.fetchone()
        if row:
            next_seq = row[0]
            conn.execute("UPDATE mail_seq SET next_seq = next_seq + 1 WHERE peer_node_id = ?", (to_node,))
        else:
            next_seq = 1
            conn.execute("INSERT INTO mail_seq (peer_node_id, next_seq) VALUES (?, 2)", (to_node,))
            
        conn.execute("""
            INSERT INTO mail_outbox (item_id, to_node, batch_seq, body_json, status, created_at, ttl_expires)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (item_id, to_node, next_seq, body_json, now, ttl_expires))
        conn.commit()
        return next_seq

    def get_pending_outbox(self, to_node: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns pending items for a peer, ordered by batch_seq."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT item_id, batch_seq, body_json, created_at, ttl_expires
            FROM mail_outbox
            WHERE to_node = ? AND status = 'pending'
            ORDER BY batch_seq ASC
            LIMIT ?
        """, (to_node, limit))
        return [
            {"item_id": r[0], "batch_seq": r[1], "body_json": r[2], "created_at": r[3], "ttl_expires": r[4]}
            for r in cursor.fetchall()
        ]

    def mark_outbox_sending(self, item_ids: List[str]):
        """Sets status pending -> sending."""
        if not item_ids: return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(f"UPDATE mail_outbox SET status = 'sending', last_attempt = ? WHERE item_id IN ({placeholders})",
                     (time.time(), *item_ids))
        conn.commit()

    def mark_outbox_delivered(self, item_ids: List[str]):
        """Sets status -> delivered. Legacy — use mark_outbox_delivered_for_peer for ownership binding."""
        if not item_ids: return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(f"UPDATE mail_outbox SET status = 'delivered', delivered_at = ? WHERE item_id IN ({placeholders})",
                     (time.time(), *item_ids))
        conn.commit()

    def mark_outbox_delivered_for_peer(self, item_ids: List[str], to_node: str):
        """V17-003: Sets sending -> delivered only for items owned by to_node."""
        if not item_ids: return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"UPDATE mail_outbox SET status = 'delivered', delivered_at = ? "
            f"WHERE item_id IN ({placeholders}) AND to_node = ? AND status = 'sending'",
            (time.time(), *item_ids, to_node)
        )
        conn.commit()

    def mark_outbox_pending(self, item_ids: List[str]):
        """Reverts status sending -> pending, increments retry_count."""
        if not item_ids: return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"UPDATE mail_outbox SET status = 'pending', retry_count = retry_count + 1 "
            f"WHERE item_id IN ({placeholders})", item_ids
        )
        conn.commit()

    def revert_stale_sending(self, cutoff_seconds: float = 120, max_retry: int = 5) -> tuple:
        """M-019/M-020: Recover items stuck in 'sending' state.

        Returns (reverted_count, failed_count).
        Items exceeding max_retry are moved to 'failed'.
        """
        now = time.time()
        cutoff = now - cutoff_seconds
        conn = self._get_conn()
        # Move over-retried items to 'failed'
        cur_fail = conn.execute(
            "UPDATE mail_outbox SET status = 'failed' "
            "WHERE status = 'sending' AND last_attempt < ? AND retry_count >= ?",
            (cutoff, max_retry)
        )
        failed = cur_fail.rowcount
        # Revert remaining stuck items to pending
        cur_revert = conn.execute(
            "UPDATE mail_outbox SET status = 'pending', retry_count = retry_count + 1 "
            "WHERE status = 'sending' AND last_attempt < ?",
            (cutoff,)
        )
        reverted = cur_revert.rowcount
        conn.commit()
        return (reverted, failed)

    def purge_outbox_expired(self, now: float) -> int:
        """Deletes expired items."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM mail_outbox WHERE ttl_expires < ?", (now,))
        count = cursor.rowcount
        conn.commit()
        return count

    def purge_outbox_delivered(self, cutoff: float) -> int:
        """Deletes delivered items older than cutoff."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM mail_outbox WHERE status = 'delivered' AND COALESCE(delivered_at, pull_delivered_at) < ?",
            (cutoff,)
        )
        count = cursor.rowcount
        conn.commit()
        return count

    def get_outbox_recipients(self) -> List[str]:
        """Returns distinct node_ids with pending outbox items."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT DISTINCT to_node FROM mail_outbox WHERE status = 'pending'")
        return [r[0] for r in cursor.fetchall()]

    def count_outbox(self) -> int:
        """Returns total pending+sending items."""
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM mail_outbox WHERE status IN ('pending', 'sending')").fetchone()[0]

    def store_mail_from_sync(self, item_id: str, from_node: str, to_node: str,
                             timestamp: float, body_json: str, session_id: Optional[str],
                             msg_type: str, reply_to: Optional[str], ttl_expires: float,
                             system: bool) -> bool:
        """Stores mail received from MAIL_SYNC, routing to the correct bucket. Returns False if duplicate."""
        bucket = self._mail_bucket(msg_type, system)
        logger.debug(f"MAIL_SYNC_ROUTE id={item_id[:8]} bucket={bucket} type={msg_type} from={from_node[:16]}")
        conn = self._get_conn()
        try:
            conn.execute(f"""
                INSERT INTO {bucket} (message_id, from_node, to_node, timestamp, body,
                                  session_id, msg_type, reply_to, ttl_expires, status, created_at, system, item_origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, 'sync')
            """, (item_id, from_node, to_node, timestamp, body_json,
                  session_id, msg_type, reply_to, ttl_expires, time.time(), 1 if system else 0))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # Address Book Methods

    def upsert_address(self, node_id: str, tier: str, label: Optional[str] = None,
                       last_ip: Optional[str] = None, last_port: Optional[int] = None,
                       sidecar_port: int = 0, group_id: Optional[str] = None):
        """Insert or update address book entry."""
        conn = self._get_conn()
        now = time.time()
        conn.execute("""
            INSERT INTO address_book (node_id, tier, label, last_ip, last_port, sidecar_port, group_id, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id, tier) DO UPDATE SET
                label=COALESCE(excluded.label, label),
                last_ip=COALESCE(excluded.last_ip, last_ip),
                last_port=COALESCE(excluded.last_port, last_port),
                sidecar_port=CASE WHEN excluded.sidecar_port > 0 THEN excluded.sidecar_port ELSE sidecar_port END,
                group_id=COALESCE(excluded.group_id, group_id),
                last_seen=excluded.last_seen
        """, (node_id, tier, label, last_ip, last_port, sidecar_port, group_id, now, now))
        conn.commit()

    def get_address(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Returns best tier entry (explicit > cached > remote)."""
        conn = self._get_conn()
        # Sort by tier priority: explicit=1, cached=2, remote=3 (alphabetical works: c, e, r)
        cursor = conn.execute("""
            SELECT node_id, tier, label, last_ip, last_port, sidecar_port, group_id, last_seen
            FROM address_book
            WHERE node_id = ?
            ORDER BY CASE tier WHEN 'explicit' THEN 1 WHEN 'cached' THEN 2 ELSE 3 END ASC
            LIMIT 1
        """, (node_id,))
        r = cursor.fetchone()
        if not r: return None
        return {
            "node_id": r[0], "tier": r[1], "label": r[2], "last_ip": r[3],
            "last_port": r[4], "sidecar_port": r[5], "group_id": r[6], "last_seen": r[7]
        }

    def get_addresses_by_tier(self, tier: str, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT node_id, tier, label, last_ip, last_port, sidecar_port, group_id, last_seen
            FROM address_book
            WHERE tier = ?
            ORDER BY last_seen DESC
            LIMIT ?
        """, (tier, limit))
        return [
            {"node_id": r[0], "tier": r[1], "label": r[2], "last_ip": r[3],
             "last_port": r[4], "sidecar_port": r[5], "group_id": r[6], "last_seen": r[7]}
            for r in cursor.fetchall()
        ]

    def evict_cached_addresses(self, max_entries: int = 200):
        """LRU eviction for 'cached' tier."""
        conn = self._get_conn()
        conn.execute("""
            DELETE FROM address_book 
            WHERE tier = 'cached' AND node_id NOT IN (
                SELECT node_id FROM address_book 
                WHERE tier = 'cached' 
                ORDER BY last_seen DESC 
                LIMIT ?
            )
        """, (max_entries,))
        conn.commit()

    def get_peer_encryption_key(self, node_id: str) -> str:
        """Get X25519 encryption_key for a peer. Returns '' if not available."""
        conn = self._get_conn()
        row = conn.execute("SELECT encryption_key FROM peers WHERE node_id = ?", (node_id,)).fetchone()
        return row[0] if row and row[0] else ""

    def update_peer_encryption_key(self, node_id: str, encryption_key: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE peers SET encryption_key = ? WHERE node_id = ?",
            (encryption_key, node_id)
        )
        conn.commit()

    def store_receipt(self, job_id: str, receipt_json: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE execution_log SET receipt = ? WHERE job_id = ?",
            (receipt_json, job_id)
        )
        conn.commit()

    # v0.32.0: Credit note bucket methods

    def store_credit_note(self, counterparty: str, reference: str, note_json: str):
        """Store a credit note in the local creditnote bucket.

        Used by both issuer (local storage) and recipient (on receipt).
        The reference (job_id) is stored as session_id for indexed lookup.

        Args:
            counterparty: The other party's node public key hex.
            reference:    The job_id this note covers.
            note_json:    Full signed credit note JSON string.
        """
        import uuid as _uuid
        conn = self._get_conn()
        now = time.time()
        message_id = str(_uuid.uuid4())
        ttl_expires = now + (30 * 86400)  # 30-day TTL for credit notes
        conn.execute("""
            INSERT INTO mail_creditnote
                (message_id, from_node, to_node, timestamp, body,
                 session_id, msg_type, reply_to, ttl_expires, status, created_at, system)
            VALUES (?, ?, ?, ?, ?, ?, 'knarr/commerce/credit_note', NULL, ?, 'unread', ?, 0)
        """, (message_id, counterparty, counterparty, now, note_json,
              reference, ttl_expires, now))
        conn.commit()
        logger.debug(f"CREDIT_NOTE_STORED ref={reference[:8]} counterparty={counterparty[:16]}")

    def get_credit_note_by_reference(self, reference: str) -> Optional[str]:
        """Fetch credit note JSON from mail_creditnote bucket by reference (job_id).

        The reference is stored in the session_id column.

        Returns:
            The full signed credit note JSON string, or None if not found.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT body FROM mail_creditnote WHERE session_id = ? LIMIT 1",
            (reference,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def write_receipt(self, receipt_id: str, document_type: str, timestamp: str,
                      identity: str, counterparty: str | None, order_ref: str | None,
                      proof_purpose: str, payload_json: str, signature: str | None) -> None:
        """Write a receipt to the append-only receipt_log. Silently ignores duplicates."""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """INSERT OR IGNORE INTO receipt_log
               (receipt_id, document_type, timestamp, identity, counterparty, order_ref,
                proof_purpose, payload_json, signature, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, document_type, timestamp, identity, counterparty, order_ref,
             proof_purpose, payload_json, signature, now)
        )
        conn.commit()
        logger.debug(f"RECEIPT_LOG_WRITE receipt_id={receipt_id[:16]} type={document_type}")

    def update_receipt_quality(self, task_id: str, quality_rating: int):
        """Store quality_rating from commerce receipt in execution_log."""
        conn = self._get_conn()
        # Add quality_rating column if not exists (migration)
        try:
            conn.execute("ALTER TABLE execution_log ADD COLUMN quality_rating INTEGER")
        except Exception:
            pass  # Column already exists
        conn.execute(
            "UPDATE execution_log SET quality_rating = ? WHERE job_id = ?",
            (quality_rating, task_id)
        )
        conn.commit()

    def get_execution_log_entry(self, task_id: str) -> Optional[Dict]:
        """Get execution log entry for a task_id (for refund price lookup).

        Returns caller_node_id as requester_node_id for B2/S-022 sender verification.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT job_id, skill_name, status, price, refund_total, caller_node_id "
            "FROM execution_log WHERE job_id = ?",
            (task_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "job_id": row[0], "skill_name": row[1], "status": row[2],
            "price": row[3], "refund_total": row[4] or 0.0,
            "requester_node_id": row[5],  # caller_node_id is the consumer/requester
        }

    def get_cumulative_refund(self, task_id: str) -> float:
        """B3/S-021: Get cumulative refund amount for a task."""
        conn = self._get_conn()
        # Add refund_total column if not exists (migration safety)
        try:
            conn.execute("ALTER TABLE execution_log ADD COLUMN refund_total REAL NOT NULL DEFAULT 0.0")
        except Exception:
            pass  # column already exists
        row = conn.execute(
            "SELECT refund_total FROM execution_log WHERE job_id = ?",
            (task_id,)
        ).fetchone()
        return row[0] if row and row[0] else 0.0

    def record_refund(self, task_id: str, amount: float) -> bool:
        """B3/S-021: Atomically record a refund only if it stays within the 2x cap.

        Returns True if the refund was recorded, False if it would exceed the cap.
        The check-and-update is a single SQL statement to prevent TOCTOU races.
        """
        conn = self._get_conn()
        # Add refund_total column if not exists (migration safety)
        try:
            conn.execute("ALTER TABLE execution_log ADD COLUMN refund_total REAL NOT NULL DEFAULT 0.0")
        except Exception:
            pass  # column already exists
        # Atomic: only update if refund_total + amount <= price * 2
        cursor = conn.execute(
            "UPDATE execution_log SET refund_total = refund_total + ? "
            "WHERE job_id = ? AND refund_total + ? <= price * 2",
            (amount, task_id, amount)
        )
        conn.commit()
        return cursor.rowcount > 0

    def queue_settlement(self, item_type: str, from_node: str, body: dict, priority: int = 0):
        """Add a settlement message to the queue."""
        import json, time
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO settlement_queue (item_type, from_node, body, priority, created_at) VALUES (?, ?, ?, ?, ?)",
            (item_type, from_node, json.dumps(body), priority, time.time())
        )
        conn.commit()

    def update_ledger_refund(self, peer_public_key: str, amount: float):
        """Credit note refund: increase consumer's balance (return credit)."""
        import time
        conn = self._get_conn()
        now = time.time()
        conn.execute("""
            UPDATE ledger SET
                balance = balance + ?,
                last_updated = ?
            WHERE peer_public_key = ?
        """, (amount, now, peer_public_key))
        conn.commit()

    def should_send_tab_reminder(self, peer_public_key: str, cooldown: float = 3600) -> bool:
        """Check if enough time has passed since last tab reminder to this peer."""
        import time
        conn = self._get_conn()
        row = conn.execute(
            "SELECT last_sent FROM tab_reminders WHERE peer_public_key = ?",
            (peer_public_key,)
        ).fetchone()
        now = time.time()
        if row and (now - row[0]) < cooldown:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO tab_reminders (peer_public_key, last_sent) VALUES (?, ?)",
            (peer_public_key, now)
        )
        conn.commit()
        return True

    def get_node_id_for_public_key(self, public_key: str) -> Optional[str]:
        """Look up node_id from peer table given a public key."""
        import hashlib
        # node_id = SHA-256(public_key_bytes)
        try:
            node_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
            return node_id
        except Exception:
            return None

    def get_average_quality_rating(self, provider_node_id: str) -> Optional[float]:
        """Get average quality rating from commerce receipts for a provider."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT AVG(quality_rating) FROM execution_log
            WHERE quality_rating IS NOT NULL
            AND provider_node_id = ?
            AND job_id IN (SELECT job_id FROM execution_log WHERE status = 'completed')
        """, (provider_node_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    def has_pending_settlement(self, peer_public_key: str) -> bool:
        """Check if there's already a pending settlement for this peer."""
        conn = self._get_conn()
        # B1/S-025: escape LIKE metacharacters to prevent injection
        # v0.36.0: use full key — truncation to 32 chars risks prefix collisions
        escaped_key = self._escape_like(peer_public_key)
        row = conn.execute(
            "SELECT 1 FROM settlement_queue WHERE status = 'pending' AND body LIKE ? ESCAPE '\\'",
            (f'%{escaped_key}%',)
        ).fetchone()
        return row is not None

    def _escape_like(self, s: str) -> str:
        """Escape LIKE metacharacters to prevent SQL LIKE injection."""
        return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    # ── Mail correspondent tracking (v0.26.0) ───────────────────────

    def upsert_correspondent(self, node_id: str, sent: bool = False, received: bool = False):
        """Track mail correspondence with a peer."""
        conn = self._get_conn()
        now = time.time()
        conn.execute("""
            INSERT INTO mail_correspondents (node_id, last_sent, last_received)
            VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                last_sent = CASE WHEN ? THEN ? ELSE last_sent END,
                last_received = CASE WHEN ? THEN ? ELSE last_received END
        """, (node_id,
              now if sent else None,
              now if received else None,
              sent, now,
              received, now))
        conn.commit()

    def get_correspondents(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get most recently active correspondents."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT node_id, last_sent, last_received
            FROM mail_correspondents
            ORDER BY MAX(COALESCE(last_sent, 0), COALESCE(last_received, 0)) DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [{"node_id": r[0], "last_sent": r[1], "last_received": r[2]} for r in rows]

    def get_pending_outbox_for_requester(self, requester_node_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get pending outbox items addressed TO a specific requester (for pull)."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT item_id, to_node, body_json, batch_seq
            FROM mail_outbox
            WHERE to_node = ? AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
        """, (requester_node_id, limit)).fetchall()
        return [{"item_id": r[0], "to_node": r[1], "body_json": r[2], "batch_seq": r[3]} for r in rows]

    def mark_outbox_pull_delivered(self, item_ids: List[str], requester_node_id: str):
        """Mark outbox items as delivered via pull. Identity-bound."""
        if not item_ids:
            return
        conn = self._get_conn()
        now = time.time()
        placeholders = ",".join(["?"] * len(item_ids))
        conn.execute(f"""
            UPDATE mail_outbox
            SET status = 'delivered', pull_delivered_at = ?
            WHERE item_id IN ({placeholders}) AND to_node = ?
        """, [now] + item_ids + [requester_node_id])
        conn.commit()

    def evict_stale_correspondents(self, max_age_days: int = 30, max_entries: int = 10000):
        """S-13: Evict correspondents not seen in >max_age_days, or cap at max_entries."""
        conn = self._get_conn()
        cutoff = time.time() - (max_age_days * 86400)
        conn.execute("""
            DELETE FROM mail_correspondents
            WHERE MAX(COALESCE(last_sent, 0), COALESCE(last_received, 0)) < ?
        """, (cutoff,))
        # Also cap total entries (keep most recent)
        conn.execute(f"""
            DELETE FROM mail_correspondents
            WHERE node_id NOT IN (
                SELECT node_id FROM mail_correspondents
                ORDER BY MAX(COALESCE(last_sent, 0), COALESCE(last_received, 0)) DESC
                LIMIT ?
            )
        """, (max_entries,))
        conn.commit()

    # ------------------------------------------------------------------
    # v0.37.0: DMZ quarantine CRUD (Warehouse Manager)
    # ------------------------------------------------------------------

    def quarantine_store(
        self,
        id: str,
        document_type: str,
        document_json: str,
        originator_pubkey: str,
        status: str,
        gate_results: str,
        reason: str | None,
    ) -> None:
        """Insert a document into the DMZ quarantine table."""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """INSERT OR REPLACE INTO dmz_quarantine
               (id, document_type, document_json, originator_pubkey,
                status, gate_results, reason, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, document_type, document_json, originator_pubkey,
             status, gate_results, reason, now),
        )
        conn.commit()
        logger.debug(f"DMZ_QUARANTINE_STORE id={id[:16]} type={document_type} status={status}")

    def quarantine_get(self, id: str) -> Optional[Dict]:
        """Retrieve a single quarantine entry by ID."""
        conn = self._get_conn()
        cursor = conn.execute(
            """SELECT id, document_type, document_json, originator_pubkey,
                      status, gate_results, reason, received_at,
                      promoted_at, resolved_at
               FROM dmz_quarantine WHERE id = ?""",
            (id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip(
            ["id", "document_type", "document_json", "originator_pubkey",
             "status", "gate_results", "reason", "received_at",
             "promoted_at", "resolved_at"],
            row,
        ))

    def quarantine_update_status(
        self,
        id: str,
        status: str,
        reason: str | None = None,
        promoted_at: float | None = None,
        resolved_at: float | None = None,
    ) -> None:
        """Update the status of a quarantine entry."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE dmz_quarantine
               SET status = ?,
                   reason = COALESCE(?, reason),
                   promoted_at = COALESCE(?, promoted_at),
                   resolved_at = COALESCE(?, resolved_at)
               WHERE id = ?""",
            (status, reason, promoted_at, resolved_at, id),
        )
        conn.commit()
        logger.debug(f"DMZ_QUARANTINE_UPDATE id={id[:16]} status={status}")

    def quarantine_list_pending(self) -> List[Dict]:
        """Return all quarantine entries with status='pending'."""
        return self.quarantine_list_by_status("pending")

    def quarantine_list_by_status(self, status: str) -> List[Dict]:
        """Return all quarantine entries matching the given status."""
        conn = self._get_conn()
        cursor = conn.execute(
            """SELECT id, document_type, document_json, originator_pubkey,
                      status, gate_results, reason, received_at,
                      promoted_at, resolved_at
               FROM dmz_quarantine WHERE status = ?
               ORDER BY received_at ASC""",
            (status,),
        )
        cols = [
            "id", "document_type", "document_json", "originator_pubkey",
            "status", "gate_results", "reason", "received_at",
            "promoted_at", "resolved_at",
        ]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def close(self):
        self._keepalive_conn.close()


class StorageStub:
    """Minimal stub for unit tests. In-memory SQLite with receipt_log only.

    Tests that need full Storage should use Storage(":memory:") instead.
    This stub exists for receipt-focused tests that don't need the full schema.
    """

    _RECEIPT_LOG_DDL = """
        CREATE TABLE IF NOT EXISTS receipt_log (
            receipt_id      TEXT PRIMARY KEY,
            document_type   TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            identity        TEXT NOT NULL,
            counterparty    TEXT,
            order_ref       TEXT,
            proof_purpose   TEXT NOT NULL,
            payload_json    TEXT NOT NULL,
            signature       TEXT,
            created_at      REAL NOT NULL
        )
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(self._RECEIPT_LOG_DDL)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_log_type ON receipt_log(document_type)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_log_identity ON receipt_log(identity)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_log_ts ON receipt_log(timestamp)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_log_order ON receipt_log(order_ref)")
        self._conn.commit()

    def write_receipt(self, receipt_id, document_type, timestamp, identity,
                      counterparty, order_ref, proof_purpose, payload_json, signature):
        """INSERT OR IGNORE into receipt_log. Idempotent."""
        self._conn.execute(
            """INSERT OR IGNORE INTO receipt_log
               (receipt_id, document_type, timestamp, identity, counterparty,
                order_ref, proof_purpose, payload_json, signature, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, document_type, timestamp, identity, counterparty,
             order_ref, proof_purpose, payload_json, signature, time.time()),
        )
        self._conn.commit()

    def get_receipt(self, receipt_id):
        cursor = self._conn.execute(
            "SELECT receipt_id, document_type, timestamp, identity, counterparty, "
            "order_ref, proof_purpose, payload_json, signature, created_at "
            "FROM receipt_log WHERE receipt_id = ?", (receipt_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip(["receipt_id", "document_type", "timestamp", "identity",
                         "counterparty", "order_ref", "proof_purpose",
                         "payload_json", "signature", "created_at"], row))

    def get_receipts_by_type(self, document_type):
        cursor = self._conn.execute(
            "SELECT receipt_id, document_type, timestamp, identity, counterparty, "
            "order_ref, proof_purpose, payload_json, signature, created_at "
            "FROM receipt_log WHERE document_type = ? ORDER BY created_at ASC",
            (document_type,))
        cols = ["receipt_id", "document_type", "timestamp", "identity",
                "counterparty", "order_ref", "proof_purpose",
                "payload_json", "signature", "created_at"]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def count_receipts(self, document_type=None):
        if document_type:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM receipt_log WHERE document_type = ?",
                (document_type,))
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM receipt_log")
        return cursor.fetchone()[0]