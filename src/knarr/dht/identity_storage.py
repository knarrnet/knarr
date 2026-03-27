"""E-05: Per-identity storage instantiation.

For each configured identity: create data directory, load/generate Ed25519 keypair
from vault, instantiate Storage, optionally wrap with StorageCacheProxy, create
identity EventBus, return Identity object for registration in IdentityRegistry.

Separate DB per identity — permanent architectural decision.
Each identity gets its own SQLite files in {base_data_dir}/identity-{name}/.
"""
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .eventbus import EventBus
from .identities import Identity

logger = logging.getLogger(__name__)


def _generate_or_load_signing_key(vault, identity_name: str):
    """Load Ed25519 signing key from vault or generate a new one.

    Key is stored in vault under 'identity:{name}:signing_key'.
    Returns (signing_key, public_key_hex).
    """
    from nacl.signing import SigningKey

    vault_key = f"identity:{identity_name}:signing_key"
    if vault is not None:
        try:
            raw_hex = vault.get(vault_key)
            if raw_hex:
                sk_bytes = bytes.fromhex(raw_hex)
                sk = SigningKey(sk_bytes)
                pub_hex = sk.verify_key.encode().hex()
                return sk, pub_hex
        except Exception as e:
            logger.warning(f"IDENTITY_KEY_LOAD_FAIL name={identity_name}: {e}")

    # Generate new key
    sk = SigningKey.generate()
    sk_hex = sk.encode().hex()
    pub_hex = sk.verify_key.encode().hex()

    if vault is not None:
        try:
            vault.set(vault_key, sk_hex)
        except Exception as e:
            logger.warning(f"IDENTITY_KEY_SAVE_FAIL name={identity_name}: {e}")

    return sk, pub_hex


def instantiate_identity(
    identity_cfg: dict,
    base_data_dir: Path,
    vault=None,
    cache_config: Optional[dict] = None,
    bus_size: int = 256,
    debug: bool = False,
) -> Identity:
    """E-05: Instantiate a single Identity from config.

    Creates the data directory, loads/generates keys, opens Storage,
    optionally wraps with StorageCacheProxy, creates EventBus.

    Args:
        identity_cfg:    Dict with keys: name, data_dir, skills, (optionally debug)
        base_data_dir:   Root data directory for the node.
        vault:           Vault instance for storing/loading keys.
        cache_config:    Optional TTL config for StorageCacheProxy.
        bus_size:        EventBus ring size for this identity.
        debug:           Enable structured debug logging.

    Returns:
        Identity instance (not yet registered — caller registers in IdentityRegistry).
    """
    name = identity_cfg["name"]
    rel_data_dir = identity_cfg.get("data_dir", f"identity-{name}")
    _debug = identity_cfg.get("debug", debug)

    # Resolve data directory
    if os.path.isabs(rel_data_dir):
        identity_dir = Path(rel_data_dir)
    else:
        identity_dir = base_data_dir / rel_data_dir

    identity_dir.mkdir(parents=True, exist_ok=True)

    if _debug:
        logger.info(f"IDENTITY_STORAGE_INIT name={name} dir={identity_dir}")

    # Load/generate signing key
    import hashlib
    signing_key, public_key_hex = _generate_or_load_signing_key(vault, name)
    # node_id = SHA-256 of public verify key (consistent with core derivation)
    node_id = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()

    if _debug:
        logger.info(f"IDENTITY_KEY_LOADED name={name} node_id={node_id[:16]}")

    # Instantiate Storage for this identity
    from .storage import Storage
    db_path = str(identity_dir / "node.db")
    storage = Storage(db_path)

    # Optionally wrap with cache proxy
    if cache_config is not None:
        try:
            import sys
            # StorageCacheProxy lives in plugins — try dynamic import
            from importlib import import_module
            _cache_mod = None
            for mod_name in sys.modules:
                if "storage-strategy" in mod_name or "cache" in mod_name:
                    m = sys.modules[mod_name]
                    if hasattr(m, "StorageCacheProxy"):
                        _cache_mod = m
                        break
            if _cache_mod:
                storage = _cache_mod.StorageCacheProxy(storage, cache_config)
                if _debug:
                    logger.info(f"IDENTITY_STORAGE_CACHE_WRAPPED name={name}")
        except Exception as e:
            logger.warning(f"IDENTITY_CACHE_WRAP_FAIL name={name}: {e} — using plain storage")

    # Create identity-scoped EventBus
    bus = EventBus(size=bus_size, debug=_debug)

    # TP-4: Build skills dict from config's skills list (was hardcoded to {})
    _skills_list = identity_cfg.get("skills", [])
    _skills_dict = {s: True for s in _skills_list} if _skills_list else {}

    identity = Identity(
        name=name,
        node_id=node_id,
        public_key_hex=public_key_hex,
        signing_key=signing_key,
        storage=storage,
        vault=vault,
        bus=bus,
        skills=_skills_dict,
        plugins=set(),
        data_dir=str(identity_dir),
        debug=_debug,
    )

    if _debug:
        logger.info(f"IDENTITY_INSTANTIATED name={name} node_id={node_id[:16]}")

    return identity


def setup_identities(
    identity_configs: list,
    base_data_dir: Path,
    vault=None,
    registry=None,
    cache_config: Optional[dict] = None,
    bus_size: int = 256,
    debug: bool = False,
) -> list:
    """E-05: Instantiate all configured identities and register in registry.

    Args:
        identity_configs:  List of identity config dicts from parse_identity_configs().
        base_data_dir:     Node's root data directory.
        vault:             Vault instance.
        registry:          IdentityRegistry to register into. If None, returns list only.
        cache_config:      Optional cache TTL config.
        bus_size:          EventBus size per identity.
        debug:             Enable debug logging.

    Returns:
        List of instantiated Identity objects.
    """
    identities = []
    for cfg in identity_configs:
        try:
            identity = instantiate_identity(
                cfg, base_data_dir, vault=vault,
                cache_config=cache_config, bus_size=bus_size, debug=debug
            )
            if registry is not None:
                registry.register(identity)
            identities.append(identity)
        except Exception as e:
            logger.error(f"IDENTITY_SETUP_FAIL name={cfg.get('name', '?')}: {e}", exc_info=True)

    return identities
