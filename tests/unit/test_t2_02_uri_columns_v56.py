import json
import sqlite3
import time
from pathlib import Path

from knarr.core.migrations import run_migrations
from knarr.core.models import SkillSheet, Task
from knarr.dht.storage import Storage


def _has_column(conn, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def test_v56_migration_adds_uri_columns_and_backfills_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY, applied_at REAL NOT NULL)")

        migrations_dir = Path(__file__).resolve().parents[2] / "src" / "knarr" / "migrations"
        for path in sorted(migrations_dir.glob("v*.sql")):
            if path.stem != "v0_56_0":
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (path.stem, time.time()),
                )

        conn.execute("CREATE TABLE peers (node_id TEXT PRIMARY KEY, host TEXT, port INTEGER, last_seen REAL)")
        conn.execute(
            "CREATE TABLE skills (skill_key TEXT, provider_node_id TEXT, skill_record_json TEXT, announced_at REAL, ttl INTEGER, is_own INTEGER)"
        )
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, skill_name TEXT, requester_node_id TEXT, provider_node_id TEXT, status TEXT, input_data_json TEXT, output_data_json TEXT, error_json TEXT, created_at REAL, updated_at REAL, timeout_ms INTEGER, provider_public_key TEXT)"
        )
        conn.execute(
            "CREATE TABLE execution_log (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, skill_name TEXT, caller_node_id TEXT, status TEXT, wall_time_ms INTEGER, input_hash TEXT, asset_hash TEXT, error TEXT, created_at REAL, price REAL, price_breakdown TEXT, provider_node_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE async_jobs (job_id TEXT PRIMARY KEY, skill_name TEXT, consumer_node_id TEXT, input_hash TEXT, status TEXT, queue_position INTEGER, result_json TEXT, error_json TEXT, created_at REAL, updated_at REAL, expires_at REAL, provider_node_id TEXT, provider_host TEXT, provider_port INTEGER, provider_public_key TEXT)"
        )
        conn.execute(
            "CREATE TABLE mail_inbox (rowid INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL, from_node TEXT NOT NULL, to_node TEXT NOT NULL, timestamp REAL NOT NULL, body TEXT NOT NULL, session_id TEXT, msg_type TEXT, reply_to TEXT, ttl_expires REAL NOT NULL, status TEXT, created_at REAL NOT NULL, system INTEGER DEFAULT 0, item_origin TEXT DEFAULT 'skill')"
        )
        conn.execute(
            "CREATE TABLE mail_outbox (item_id TEXT PRIMARY KEY, to_node TEXT NOT NULL, batch_seq INTEGER NOT NULL, body_json TEXT NOT NULL, status TEXT, created_at REAL NOT NULL, delivered_at REAL, ttl_expires REAL NOT NULL, retry_count INTEGER DEFAULT 0, last_attempt REAL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE settlement_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT NOT NULL, from_node TEXT NOT NULL, body TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, processed_at REAL)"
        )

        provider = "a" * 64
        inbox_to = "b" * 64
        outbox_from = "c" * 64
        conn.execute(
            "INSERT INTO skills (skill_key, provider_node_id, skill_record_json, announced_at, ttl, is_own) VALUES (?, ?, ?, ?, ?, ?)",
            ("echo", provider, "{}", time.time(), 60, 0),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, skill_name, requester_node_id, provider_node_id, status, input_data_json, created_at, updated_at, timeout_ms, provider_public_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task-1", "echo", "r" * 64, provider, "submitted", "{}", time.time(), time.time(), 30000, ""),
        )
        conn.execute(
            "INSERT INTO execution_log (job_id, skill_name, caller_node_id, status, wall_time_ms, input_hash, asset_hash, error, created_at, price, price_breakdown, provider_node_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-1", "echo", "r" * 64, "completed", 5, "ih", "ah", "", time.time(), 1.0, "", provider),
        )
        conn.execute(
            "INSERT INTO async_jobs (job_id, skill_name, consumer_node_id, input_hash, status, queue_position, created_at, updated_at, expires_at, provider_node_id, provider_host, provider_port, provider_public_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-2", "echo", "r" * 64, "hash", "remote", 0, time.time(), time.time(), time.time() + 60, provider, "127.0.0.1", 9000, ""),
        )
        conn.execute(
            "INSERT INTO mail_inbox (message_id, from_node, to_node, timestamp, body, session_id, msg_type, reply_to, ttl_expires, status, created_at, system, item_origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("msg-1", "f" * 64, inbox_to, time.time(), "{}", None, "text", None, time.time() + 60, "unread", time.time(), 0, "skill"),
        )
        conn.execute(
            "INSERT INTO mail_outbox (item_id, to_node, batch_seq, body_json, status, created_at, ttl_expires, retry_count, last_attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("msg-2", "d" * 64, 1, json.dumps({"from_node": outbox_from}), "pending", time.time(), time.time() + 60, 0, 0.0),
        )
        conn.execute(
            "INSERT INTO settlement_queue (item_type, from_node, body, priority, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("soft_threshold", "e" * 64, json.dumps({"peer_public_key": "pk"}), 2, "pending", time.time()),
        )
        conn.commit()

        count = run_migrations(conn, str(migrations_dir))
        assert count == 1
        assert run_migrations(conn, str(migrations_dir)) == 0

        for table in ("skills", "tasks", "execution_log", "async_jobs", "mail_inbox", "mail_outbox", "settlement_queue"):
            assert _has_column(conn, table, "uri")

        assert _has_column(conn, "peers", "tls_cert_fingerprint")
        assert conn.execute("SELECT uri FROM skills WHERE skill_key = 'echo'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM tasks WHERE task_id = 'task-1'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM execution_log WHERE job_id = 'job-1'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM async_jobs WHERE job_id = 'job-2'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM mail_inbox WHERE message_id = 'msg-1'").fetchone()[0] == f"knarr://{inbox_to}/m/msg-1"
        assert conn.execute("SELECT uri FROM mail_outbox WHERE item_id = 'msg-2'").fetchone()[0] == f"knarr://{outbox_from}/m/msg-2"
        assert conn.execute("SELECT uri FROM settlement_queue WHERE id = 1").fetchone()[0] == f"knarr://{'e' * 64}/c/1"
    finally:
        conn.close()


def test_storage_helpers_populate_v56_uri_columns_on_new_writes(tmp_path):
    storage = Storage(str(tmp_path / "node.db"))
    try:
        provider = "a" * 64
        requester = "b" * 64
        inbox_to = "c" * 64
        outbox_from = "d" * 64
        settlement_counterparty = "e" * 64

        skill = SkillSheet(
            name="echo",
            version="1.0.0",
            description="Echo",
            tags=["test"],
            input_schema={},
            output_schema={},
        )
        storage.upsert_skill("echo", provider, skill)

        task = Task(
            task_id="task-new",
            skill_name="echo",
            requester_node_id=requester,
            provider_node_id=provider,
            status="submitted",
            input_data={"text": "hello"},
            created_at=time.time(),
            updated_at=time.time(),
        )
        storage.insert_task(task)
        storage.log_execution("job-new", "echo", requester, "completed", 7, provider_node_id=provider)
        storage.insert_async_job("async-local", "echo", requester, "hash-1", 1, time.time() + 60, provider)
        storage.insert_remote_job("async-remote", "echo", provider, "127.0.0.1", 9010, time.time() + 60)
        storage.store_mail("mail-1", requester, inbox_to, time.time(), "{}", None, "text", None, time.time() + 60)
        storage.enqueue_outbox(
            "mail-2",
            requester,
            json.dumps({"from_node": outbox_from, "to_node": requester}),
            time.time() + 60,
            outbox_from,
        )
        storage.queue_settlement(
            "soft_threshold",
            from_node=provider,
            body={"type": "netting_trigger"},
            priority=2,
            counterparty_node_id=settlement_counterparty,
        )

        conn = storage._get_conn()
        assert conn.execute("SELECT uri FROM skills WHERE skill_key = 'echo'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM tasks WHERE task_id = 'task-new'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM execution_log WHERE job_id = 'job-new'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM async_jobs WHERE job_id = 'async-local'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM async_jobs WHERE job_id = 'async-remote'").fetchone()[0] == f"knarr://{provider}/s/echo"
        assert conn.execute("SELECT uri FROM mail_inbox WHERE message_id = 'mail-1'").fetchone()[0] == f"knarr://{inbox_to}/m/mail-1"
        assert conn.execute("SELECT uri FROM mail_outbox WHERE item_id = 'mail-2'").fetchone()[0] == f"knarr://{outbox_from}/m/mail-2"
        settlement_uri = conn.execute("SELECT uri FROM settlement_queue ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert settlement_uri == f"knarr://{settlement_counterparty}/c/1"
    finally:
        storage.close()
