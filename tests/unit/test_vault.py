import os
import pytest
import tomllib
from knarr.core.vault import KeyringVault
from nacl.exceptions import CryptoError

@pytest.fixture
def seed():
    return os.urandom(32)

@pytest.fixture
def vault_db(tmp_path):
    return str(tmp_path / "vault.db")

def test_vault_roundtrip(vault_db, seed):
    """Create vault with a known seed, set a value, close, reopen with same seed, read value back."""
    vault = KeyringVault(vault_db, seed)
    vault.set("test_scope", "test_key", "test_value")
    vault.close()
    
    vault2 = KeyringVault(vault_db, seed)
    assert vault2.get("test_scope", "test_key") == "test_value"
    vault2.close()

def test_vault_overwrite(vault_db, seed):
    """Set same (scope, key) twice with different values. Assert second value wins."""
    vault = KeyringVault(vault_db, seed)
    vault.set("test_scope", "test_key", "first_value")
    vault.set("test_scope", "test_key", "second_value")
    assert vault.get("test_scope", "test_key") == "second_value"
    vault.close()

def test_vault_delete(vault_db, seed):
    """Set a value, delete it, assert get() returns None."""
    vault = KeyringVault(vault_db, seed)
    vault.set("test_scope", "test_key", "to_be_deleted")
    vault.delete("test_scope", "test_key")
    assert vault.get("test_scope", "test_key") is None
    vault.close()

def test_vault_list_scopes(vault_db, seed):
    """Set values in 3 different scopes, assert list_scopes() returns all 3."""
    vault = KeyringVault(vault_db, seed)
    vault.set("scope1", "k1", "v1")
    vault.set("scope2", "k2", "v2")
    vault.set("scope3", "k3", "v3")
    
    scopes = vault.list_scopes()
    assert len(scopes) == 3
    assert "scope1" in scopes
    assert "scope2" in scopes
    assert "scope3" in scopes
    vault.close()

def test_vault_migrate_from_toml(vault_db, seed, tmp_path):
    """Write a temporary secrets.toml file with known content, call migrate_from_toml()."""
    toml_path = tmp_path / "secrets.toml"
    content = """
[skill1]
api_key = "secret1"
timeout = 30

[SKILL2]
token = "secret2"
"""
    toml_path.write_text(content)
    
    vault = KeyringVault(vault_db, seed)
    count = vault.migrate_from_toml(str(toml_path))
    assert count == 3
    
    assert vault.get("skill1", "api_key") == "secret1"
    assert vault.get("skill1", "timeout") == "30"
    assert vault.get("skill2", "token") == "secret2"
    vault.close()

def test_vault_wrong_seed_fails(vault_db, seed):
    """Create vault with seed A, set a value. Close. Reopen with seed B. Assert CryptoError."""
    vault = KeyringVault(vault_db, seed)
    vault.set("scope", "key", "value")
    vault.close()
    
    wrong_seed = os.urandom(32)
    vault2 = KeyringVault(vault_db, wrong_seed)
    with pytest.raises(CryptoError):
        vault2.get("scope", "key")
    vault2.close()

def test_vault_empty_fresh(vault_db, seed):
    """Open a fresh vault, assert list_scopes() is empty and has_entries() is False."""
    vault = KeyringVault(vault_db, seed)
    assert vault.list_scopes() == []
    assert vault.has_entries() is False
    vault.close()

def test_vault_unicode(vault_db, seed):
    """Set values containing unicode characters. Assert roundtrip preserves them exactly."""
    vault = KeyringVault(vault_db, seed)
    unicode_val = "Accented: éàç, CJK: ⚡, Emoji: 🔒🔑"
    vault.set("unicode", "key", unicode_val)
    assert vault.get("unicode", "key") == unicode_val
    vault.close()
