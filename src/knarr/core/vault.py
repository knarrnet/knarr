import logging
import os
import sqlite3
import stat
import time
import tomllib
from typing import Optional

from nacl.pwhash import argon2id
from nacl.secret import SecretBox
from nacl.utils import random

logger = logging.getLogger(__name__)

VAULT_VERSION = 1
SALT_SIZE = argon2id.SALTBYTES  # 16 bytes

class KeyringVault:
    """Encrypted-at-rest secret storage backed by SQLite + NaCl SecretBox."""

    def __init__(self, db_path: str, seed_bytes: bytes):
        """Open or create a vault. Derives encryption key from seed via Argon2id."""
        self.db_path = db_path
        is_new = not os.path.exists(db_path)
        # Open SQLite with check_same_thread=False + WAL + busy_timeout
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # V21-005: Restrict file permissions on Unix (owner-only read/write)
        if is_new and hasattr(os, 'chmod'):
            try:
                os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            except OSError:
                pass  # Windows ACLs — best-effort
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        
        self._init_db()
        
        # Get or create random salt in vault_meta
        salt = self._get_meta("salt")
        if salt is None:
            salt = random(SALT_SIZE)
            self._set_meta("salt", salt)
            self._set_meta("version", VAULT_VERSION.to_bytes(4, 'big'))
        else:
            # Check version compatibility
            stored_ver = self._get_meta("version")
            if stored_ver:
                ver = int.from_bytes(stored_ver, 'big')
                if ver > VAULT_VERSION:
                    raise ValueError(f"Vault version {ver} is newer than supported {VAULT_VERSION}")
        
        # Derive vault key
        vault_key = argon2id.kdf(
            SecretBox.KEY_SIZE, seed_bytes, salt,
            opslimit=argon2id.OPSLIMIT_MODERATE,
            memlimit=argon2id.MEMLIMIT_MODERATE
        )
        self._box = SecretBox(vault_key)

    def _init_db(self):
        cursor = self._conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vault_meta (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vault_entries (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                encrypted_value BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, key)
            );
        """)
        self._conn.commit()

    def _get_meta(self, key: str) -> Optional[bytes]:
        cursor = self._conn.execute("SELECT value FROM vault_meta WHERE key = ?", (key,))
        row = cursor.fetchone()
        return bytes(row[0]) if row else None

    def _set_meta(self, key: str, value: bytes):
        self._conn.execute("INSERT OR REPLACE INTO vault_meta (key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def get(self, scope: str, key: str) -> Optional[str]:
        """Decrypt and return a single secret. Returns None if not found."""
        cursor = self._conn.execute(
            "SELECT encrypted_value FROM vault_entries WHERE scope = ? AND key = ?",
            (scope.lower(), key)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._box.decrypt(bytes(row[0])).decode('utf-8')

    def get_all(self, scope: str) -> dict[str, str]:
        """Decrypt and return all secrets for a scope."""
        cursor = self._conn.execute(
            "SELECT key, encrypted_value FROM vault_entries WHERE scope = ?",
            (scope.lower(),)
        )
        results = {}
        for row in cursor.fetchall():
            try:
                results[row['key']] = self._box.decrypt(bytes(row['encrypted_value'])).decode('utf-8')
            except Exception as e:
                logger.warning(f"VAULT_DECRYPT_FAIL scope={scope} key={row['key']}: {e}")
                continue
        return results

    def set(self, scope: str, key: str, value: str):
        """Encrypt and store a secret. INSERT OR REPLACE."""
        encrypted = self._box.encrypt(value.encode('utf-8'))
        updated_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self._conn.execute(
            "INSERT OR REPLACE INTO vault_entries (scope, key, encrypted_value, updated_at) VALUES (?, ?, ?, ?)",
            (scope.lower(), key, encrypted, updated_at)
        )
        self._conn.commit()

    def encrypt_bytes(self, data: bytes) -> bytes:
        """SA-04: Encrypt raw bytes with the vault's SecretBox."""
        return bytes(self._box.encrypt(data))

    def decrypt_bytes(self, data: bytes) -> bytes:
        """SA-04: Decrypt raw bytes with the vault's SecretBox."""
        return bytes(self._box.decrypt(data))

    def delete(self, scope: str, key: str):
        """Remove a secret entry."""
        self._conn.execute(
            "DELETE FROM vault_entries WHERE scope = ? AND key = ?",
            (scope.lower(), key)
        )
        self._conn.commit()

    def list_scopes(self) -> list[str]:
        """Return all distinct scopes that have entries."""
        cursor = self._conn.execute("SELECT DISTINCT scope FROM vault_entries")
        return [row[0] for row in cursor.fetchall()]

    def has_entries(self) -> bool:
        """Return True if the vault contains any entries."""
        cursor = self._conn.execute("SELECT 1 FROM vault_entries LIMIT 1")
        return cursor.fetchone() is not None

    def migrate_from_toml(self, toml_path: str) -> int:
        """Import secrets from a plaintext secrets.toml. Returns count of values migrated."""
        if not os.path.exists(toml_path):
            return 0
        
        with open(toml_path, "rb") as f:
            try:
                data = tomllib.load(f)
            except Exception as e:
                logger.error(f"Failed to load TOML for migration: {e}")
                return 0
        
        count = 0
        for scope, entries in data.items():
            if isinstance(entries, dict):
                for key, value in entries.items():
                    self.set(scope.lower(), key, str(value))
                    count += 1
        
        logger.info(f"VAULT_MIGRATE source={toml_path} count={count}")
        return count

    def close(self):
        """Close the SQLite connection."""
        self._conn.close()
