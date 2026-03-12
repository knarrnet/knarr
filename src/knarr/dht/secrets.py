"""
SecretsManager — per-skill secrets loading and injection (v0.43.0 C2).

Extracted from DHTNode._inject_secrets + load_secrets to reduce node.py size.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class SecretsManager:
    """Manages per-skill secrets backed by a KeyringVault.

    Args:
        secrets_path: Optional path to legacy secrets.toml for migration.
    """

    def __init__(self, secrets_path: str = ""):
        self._secrets_path = secrets_path
        self._secrets: Dict[str, Dict[str, str]] = {}
        self._vault = None

    def set_vault(self, vault) -> None:
        """Wire in vault after node identity is loaded. Must be called before load()."""
        self._vault = vault

    def load(self, secrets_path: str = "") -> None:
        """Load per-skill secrets from vault. Auto-migrates from secrets.toml on first run."""
        self._secrets.clear()
        path = secrets_path or self._secrets_path

        if not self._vault:
            logger.debug("SECRETS_LOAD_SKIP: no vault available")
            return

        # Auto-migrate from secrets.toml (idempotent — uses upsert, safe to re-run)
        if path and os.path.exists(path):
            try:
                count = self._vault.migrate_from_toml(path)
                if count > 0:
                    logger.info(f"Migrated {count} secret(s) from secrets.toml to vault")
            except Exception as e:
                logger.error(f"Failed to migrate secrets.toml: {e}")
            else:
                # Only rename after successful migration (separate from try block)
                try:
                    migrated_path = path + ".migrated"
                    os.rename(path, migrated_path)
                except OSError as e:
                    logger.warning(f"Could not rename secrets.toml after migration: {e} — plaintext file persists")

        # Load all secrets from vault into memory
        for scope in self._vault.list_scopes():
            self._secrets[scope.lower()] = self._vault.get_all(scope)
        if self._secrets:
            logger.info(f"SECRETS_LOADED: {len(self._secrets)} skill(s) from vault")
        else:
            logger.debug("SECRETS_LOAD_EMPTY: no secrets in vault")

    def inject(self, skill_name: str, input_data: dict) -> dict:
        """Inject per-skill secrets into input_data. Caller values take precedence.

        Returns a new dict with secrets merged in (caller values not overwritten).
        """
        secrets = self._secrets.get(skill_name.lower())
        if not secrets:
            return input_data
        merged = dict(input_data)
        for k, v in secrets.items():
            if k not in merged:
                merged[k] = v
        logger.debug(f"SECRETS_INJECTED: skill={skill_name!r} keys={list(secrets.keys())}")
        return merged

    def get_summary(self) -> dict:
        """Returns per-skill secret status for cockpit (values masked)."""
        result = {}
        for skill_name, secrets in self._secrets.items():
            result[skill_name] = {
                k: {"filled": bool(v), "masked": f"***({len(v)} chars)" if v else "***"}
                for k, v in secrets.items()
            }
        return result

    def set_secret(self, skill_name: str, key: str, value: str) -> None:
        """Set a secret value and persist to vault."""
        skill_name = skill_name.lower()
        if skill_name not in self._secrets:
            self._secrets[skill_name] = {}
        self._secrets[skill_name][key] = value
        if self._vault:
            self._vault.set(skill_name, key, value)

    def delete_secret(self, skill_name: str, key: str) -> None:
        """Delete a secret value from vault."""
        skill_name = skill_name.lower()
        if skill_name in self._secrets and key in self._secrets[skill_name]:
            del self._secrets[skill_name][key]
            if not self._secrets[skill_name]:
                del self._secrets[skill_name]
        if self._vault:
            self._vault.delete(skill_name, key)
