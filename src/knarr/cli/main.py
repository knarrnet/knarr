import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import shlex
import signal
import sys
import os
from typing import List, Optional, Dict, Any
from pathlib import Path
from dataclasses import asdict

from ..dht.node import DHTNode
from ..dht.storage import Storage
from .config import load_config, load_handler, detect_advertise_host, is_private_ip, _cleanup_handler_module
from .init import init_project
from ..core.models import Policy, GroupPolicy, SkillPolicy
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)


def resolve_cockpit_token(config_dir: str, configured_token: str) -> str:
    """Return a cockpit auth token, auto-generating and persisting one if needed.

    If configured_token is non-empty, returns it as-is.
    Otherwise reads from .cockpit_token in config_dir, or generates a new one.
    """
    if configured_token:
        return configured_token
    import secrets as _secrets
    token_path = os.path.join(config_dir, ".cockpit_token")
    token = ""
    if os.path.isfile(token_path):
        try:
            with open(token_path, "r") as f:
                token = f.read().strip()
        except Exception:
            pass
    if not token:
        token = f"knarr-{_secrets.token_hex(6)}"
        try:
            with open(token_path, "w") as f:
                f.write(token)
        except Exception as e:
            logger.warning(f"Could not save cockpit token: {e}")
    return token


def _resolve_data_dir(cli_data_dir: Optional[str], config: Dict[str, Any], config_dir: str) -> tuple[str, bool]:
    env_data_dir = os.getenv("KNARR_DATA_DIR")
    cfg_data_dir = config.get("node", {}).get("data_dir")

    if cli_data_dir:
        return os.path.abspath(cli_data_dir), True
    if env_data_dir:
        return os.path.abspath(env_data_dir), True
    if cfg_data_dir:
        if os.path.isabs(cfg_data_dir):
            return cfg_data_dir, True
        return os.path.abspath(os.path.join(config_dir, cfg_data_dir)), True
    return config_dir, False


def _resolve_storage_path(cli_storage: Optional[str], config: Dict[str, Any], data_dir: str) -> str:
    if cli_storage is not None:
        return cli_storage
    configured = config.get("node", {}).get("storage")
    if not configured:
        return os.path.join(data_dir, "node.db")
    if os.path.isabs(configured):
        return configured
    return os.path.join(data_dir, configured)


def _warn_duplicate_identity_files(config_dir: str, data_dir: str) -> None:
    if os.path.abspath(config_dir) == os.path.abspath(data_dir):
        return
    config_identity = all(os.path.exists(os.path.join(config_dir, name)) for name in ("key.pem", "cert.pem"))
    data_identity = all(os.path.exists(os.path.join(data_dir, name)) for name in ("key.pem", "cert.pem"))
    if config_identity and data_identity:
        logger.warning(
            "Identity files found in both config_dir and data_dir. Using data_dir. Remove duplicates to silence this warning."
        )


def _log_operator_backup_instruction(data_dir: str) -> None:
    logger.warning(
        "=== OPERATOR ACTION REQUIRED ===\n"
        "Your node identity and wallet keys have been created at: %s\n"
        "Back up the following files to a secure off-site location:\n\n"
        "  IDENTITY (irreplaceable — your reputation, credit, and trust):\n"
        "    %s\n"
        "    %s\n\n"
        "  WALLETS (on-chain asset custody):\n"
        "    %s\n"
        "    %s\n\n"
        "Loss of identity files means loss of all credit relationships and network reputation.\n"
        "Loss of wallet files means loss of on-chain assets.\n"
        "=== END ===",
        data_dir,
        os.path.join(data_dir, "key.pem"),
        os.path.join(data_dir, "cert.pem"),
        os.path.join(data_dir, "secrets.toml"),
        os.path.join(data_dir, "plugin_state", "06-thrall", "thrall_identity.key"),
    )

async def upload_asset(host: str, port: int, data: bytes, signing_key: SigningKey) -> str:
    """Uploads data to sidecar and returns content hash."""
    import time
    from ..mail.tls import create_client_ssl_context
    content_hash = hashlib.sha256(data).hexdigest()
    timestamp = str(int(time.time()))
    pub_key_hex = signing_key.verify_key.encode().hex()

    payload = f"PUT:/assets:{timestamp}:{content_hash}".encode("utf-8")
    signature = signing_key.sign(payload).signature.hex()

    ssl_ctx = create_client_ssl_context()
    try:
        reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
    except (ConnectionRefusedError, OSError) as e:
        raise Exception(f"Sidecar unreachable at {host}:{port} — {e}. Check that the provider's sidecar is running and the port is accessible.")
    try:
        headers = (
            f"PUT /assets HTTP/1.1\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature}\r\n"
            f"x-knarr-timestamp: {timestamp}\r\n"
            f"x-knarr-content-hash: {content_hash}\r\n\r\n"
        ).encode()
        writer.write(headers + data)
        await writer.drain()
        
        # Read status line
        line = await reader.readline()
        if b"200 OK" not in line:
            raise Exception(f"Upload failed: {line.decode().strip()}")
            
        # Skip headers
        while True:
            line = await reader.readline()
            if line == b"\r\n": break
            
        return content_hash
    finally:
        writer.close()
        await writer.wait_closed()

async def download_asset(host: str, port: int, hash: str, signing_key: SigningKey) -> bytes:
    """Downloads data from sidecar."""
    import time
    from ..mail.tls import create_client_ssl_context
    timestamp = str(int(time.time()))
    pub_key_hex = signing_key.verify_key.encode().hex()

    payload = f"GET:/assets/{hash}:{timestamp}:empty".encode("utf-8")
    signature = signing_key.sign(payload).signature.hex()

    ssl_ctx = create_client_ssl_context()
    try:
        reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
    except (ConnectionRefusedError, OSError) as e:
        raise Exception(f"Sidecar unreachable at {host}:{port} — {e}. Check that the provider's sidecar is running and the port is accessible.")
    try:
        req = (
            f"GET /assets/{hash} HTTP/1.1\r\n"
            f"x-knarr-publickey: {pub_key_hex}\r\n"
            f"x-knarr-signature: {signature}\r\n"
            f"x-knarr-timestamp: {timestamp}\r\n\r\n"
        ).encode()
        writer.write(req)
        await writer.drain()

        line = await reader.readline()
        if b"200 OK" not in line:
            status = line.decode().strip()
            if b"404" in line:
                raise Exception(f"Asset not found: {hash[:16]}... (sidecar returned 404)")
            elif b"401" in line:
                raise Exception(f"Sidecar auth failed for {host}:{port} (401). Check clock sync and key validity.")
            raise Exception(f"Sidecar download failed: {status}")
            
        content_length = 0
        while True:
            line = await reader.readline()
            if line == b"\r\n": break
            if b"Content-Length:" in line:
                content_length = int(line.decode().split(":")[1].strip())
                
        if content_length > 0:
            return await reader.readexactly(content_length)
        else:
            return await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()

def _resolve_handler_path(handler_spec: str, config_dir: str) -> Optional[str]:
    """Resolve a handler spec to an absolute file path."""
    if not handler_spec:
        return None
    file_path = handler_spec.rsplit(":", 1)[0] if ":" in handler_spec else handler_spec
    if not os.path.isabs(file_path):
        file_path = os.path.join(config_dir, file_path)
    return os.path.abspath(file_path)

def _inject_node_if_supported(handler_fn, node):
    """If the handler's module exposes set_node(node), call it."""
    module = inspect.getmodule(handler_fn)
    if module is None:
        return
    set_node = getattr(module, "set_node", None)
    if callable(set_node):
        set_node(node)

def _build_skill_sheet_data(name: str, skill_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Build skill sheet data dict from config, including v0.11.0 fields."""
    data = {
        "name": name,
        "version": skill_cfg.get("version", "1.0.0"),
        "description": skill_cfg.get("description", ""),
        "tags": skill_cfg.get("tags") or ["custom"],
        "input_schema": skill_cfg.get("input_schema", {}),
        "output_schema": skill_cfg.get("output_schema", {}),
        "price": float(skill_cfg.get("price", 1.0)),
        "max_input_size": int(skill_cfg.get("max_input_size", 65536)),
    }
    if skill_cfg.get("uri"):
        data["uri"] = skill_cfg["uri"]
    if skill_cfg.get("input_spec"):
        data["input_spec"] = skill_cfg["input_spec"]
    if skill_cfg.get("jurisdiction"):
        data["jurisdiction"] = skill_cfg["jurisdiction"]
    return data


async def load_skills_from_config(node: DHTNode, cfg: Dict[str, Any], cfg_dir: str) -> int:
    """Loads skills from config, registers handlers, and announces. Returns count loaded."""
    loaded = 0
    for name, skill_cfg in cfg.get("skills", {}).items():
        try:
            handler_spec = skill_cfg.get("handler")
            if not handler_spec:
                continue

            # Visibility config — always update, even for existing skills (hot-reload may change it)
            visibility = skill_cfg.get("visibility", "public")
            allowed_nodes = skill_cfg.get("allowed_nodes", [])
            if visibility not in ("public", "private", "whitelist"):
                print(f"Error: Invalid visibility '{visibility}' for skill '{name}'", file=sys.stderr)
                continue
            if visibility == "whitelist" and not allowed_nodes:
                print(f"Error: Skill '{name}' has visibility 'whitelist' but no allowed_nodes", file=sys.stderr)
                continue

            old_visibility = node._skill_visibility.get(name.lower(), "public")
            node._skill_visibility[name.lower()] = visibility
            node._skill_allowed_nodes[name.lower()] = allowed_nodes

            # If skill transitioned to private, deregister from DHT
            if visibility == "private" and old_visibility != "private" and name.lower() in node._handlers:
                await node.deregister(name.lower())
                logger.info(f"Deregistered '{name}' from DHT (now private)")

            # Skip handler registration + announce if already registered and unchanged
            if name.lower() in node._handlers:
                # Check if handler file changed (path or mtime)
                current_handler_spec = handler_spec
                old_spec = node._handler_specs.get(name.lower())
                
                handler_path = _resolve_handler_path(current_handler_spec, cfg_dir)
                old_mtime = node._handler_mtimes.get(name.lower(), 0)
                current_mtime = os.path.getmtime(handler_path) if handler_path and os.path.exists(handler_path) else 0

                if current_handler_spec != old_spec or current_mtime > old_mtime:
                    # Handler changed — reload
                    _cleanup_handler_module(name.lower())
                    try:
                        handler_fn = load_handler(current_handler_spec, cfg_dir, skill_name=name.lower())
                    except (ImportError, SyntaxError) as e:
                        logger.error(f"Failed to reload handler for '{name}': {e}")
                        # Keep old handler — don't crash, don't deregister
                        continue

                    _inject_node_if_supported(handler_fn, node)
                    node.register_handler(name, handler_fn)
                    node._handler_specs[name.lower()] = current_handler_spec
                    node._handler_mtimes[name.lower()] = current_mtime
                    
                    # Re-announce (metadata may have changed too)
                    skill_sheet_data = _build_skill_sheet_data(name, skill_cfg)
                    await node.announce(skill_sheet_data)
                    loaded += 1
                
                # Re-announce if visibility changed to public/whitelist from private
                elif old_visibility == "private" and visibility != "private":
                    skill_sheet_data = _build_skill_sheet_data(name, skill_cfg)
                    await node.announce(skill_sheet_data)
                    loaded += 1
                continue

            handler_fn = load_handler(handler_spec, cfg_dir, skill_name=name.lower())
            _inject_node_if_supported(handler_fn, node)
            node.register_handler(name, handler_fn)
            node._handler_specs[name.lower()] = handler_spec
            handler_path = _resolve_handler_path(handler_spec, cfg_dir)
            node._handler_mtimes[name.lower()] = os.path.getmtime(handler_path) if handler_path and os.path.exists(handler_path) else 0

            try:
                skill_sheet_data = _build_skill_sheet_data(name, skill_cfg)
                await node.announce(skill_sheet_data)
            except Exception as ann_err:
                logger.error(f"Skill '{name}' handler loaded but announcement FAILED: {ann_err}")
                print(f"ERROR: Skill '{name}' loaded locally but cannot announce to network: {ann_err}", file=sys.stderr)
            loaded += 1
        except Exception as e:
            print(f"Could not load handler for skill '{name}': {e}", file=sys.stderr)

    # Detect removed skills
    fresh_skill_names = {name.lower() for name in cfg.get("skills", {})}
    registered_names = set(node._handler_specs.keys())
    removed = registered_names - fresh_skill_names

    for name in removed:
        await node.deregister(name)
        if name in node._handlers:
            del node._handlers[name]
        _cleanup_handler_module(name)
        node._handler_specs.pop(name, None)
        node._handler_mtimes.pop(name, None)
        node._skill_visibility.pop(name, None)
        node._skill_allowed_nodes.pop(name, None)
        logger.info(f"Removed skill '{name}' (no longer in config)")

    return loaded


async def cmd_run(args):
    """Quick start: auto-init if no config, serve with cockpit, open browser."""
    config_path = Path(args.config) if args.config else Path("knarr.toml")
    if not config_path.exists():
        from .init import KNARR_TOML_TEMPLATE, ECHO_PY_TEMPLATE
        port = args.port or 9000
        config_path.write_text(KNARR_TOML_TEMPLATE.substitute(
            port=port, bootstrap="bootstrap1.knarr.network:9000"))
        skills_dir = Path("skills")
        skills_dir.mkdir(exist_ok=True)
        echo_path = skills_dir / "echo.py"
        if not echo_path.exists():
            echo_path.write_text(ECHO_PY_TEMPLATE)
        print(f"Initialized new node in {os.getcwd()}")

    # Ensure cockpit is enabled (default 8090 if not set)
    if args.cockpit is None:
        from .config import load_config
        cfg = load_config(config_path, explicit=bool(args.config))
        cockpit_port = cfg.get("cockpit", {}).get("port", 0)
        if cockpit_port <= 0:
            args.cockpit = 8090
        else:
            args.cockpit = cockpit_port

    # Run serve with browser open after start
    args._open_browser = True
    await cmd_serve(args)


async def cmd_serve(args):
    # Set up logging early
    log_level = args.log_level or "INFO"
    logging.basicConfig(level=getattr(logging, log_level), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 1. Load config
    config_path = Path(args.config) if args.config else Path("knarr.toml")
    config = load_config(config_path, explicit=bool(args.config))
    config_dir = os.path.dirname(os.path.abspath(config_path)) if config_path.exists() else os.getcwd()
    config["_config_dir"] = config_dir
    data_dir, data_dir_explicit = _resolve_data_dir(getattr(args, "data_dir", None), config, config_dir)
    os.makedirs(data_dir, exist_ok=True)
    config["_data_dir"] = data_dir
    config["_data_dir_explicit"] = data_dir_explicit
    _warn_duplicate_identity_files(config_dir, data_dir)

    # 2. Merge logic: Defaults -> Config -> CLI
    port = args.port if args.port is not None else config["node"].get("port", 9000)
    bind_host = args.host if args.host is not None else config["node"].get("host", "0.0.0.0")
    storage_path = _resolve_storage_path(args.storage, config, data_dir)
    
    bootstrap_peers = []
    if args.bootstrap:
        bootstrap_peers = [p.strip() for p in args.bootstrap.split(",") if p.strip()]
    else:
        bootstrap_peers = config["network"].get("bootstrap", [])

    # 3. Resolve advertise host
    advertise_host = args.advertise_host if args.advertise_host is not None else config["node"].get("advertise_host")
    if not advertise_host and bind_host in ("0.0.0.0", "::"):
        detected = detect_advertise_host()
        if detected:
            advertise_host = detected
            if is_private_ip(detected):
                print(f"Warning: Auto-detected address {detected} is private. Remote peers may not be able to connect. Use --advertise-host to set your public IP.", file=sys.stderr)
            else:
                logger.info(f"Auto-detected advertise address: {detected}")
        else:
            print("Error: Cannot auto-detect IP. Set --advertise-host explicitly when binding to 0.0.0.0.", file=sys.stderr)
            sys.exit(1)

    # Parse policy
    policy_cfg = config.get("policy", {})
    policy = Policy(
        initial_credit=float(policy_cfg.get("initial_credit", 3.0)),
        min_balance=float(policy_cfg.get("min_balance", -10.0)),
        tit_for_tat=bool(policy_cfg.get("tit_for_tat", False)),
    )

    # Parse group policies (NEW)
    group_policies = []
    for group_name, group_cfg in policy_cfg.get("group", {}).items():
        members = set(group_cfg.get("members", []))
        members_file = group_cfg.get("members_file")
        if members_file:
            members_file_path = os.path.join(config_dir, members_file) if not os.path.isabs(members_file) else members_file
            members_file_path = os.path.abspath(members_file_path)
            config_dir_abs = os.path.abspath(config_dir)
            if not members_file_path.startswith(config_dir_abs + os.sep) and members_file_path != config_dir_abs:
                logger.warning(f"members_file escapes config directory, skipping: {members_file}")
            elif os.path.exists(members_file_path):
                with open(members_file_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            members.add(line)
        group_policies.append(GroupPolicy(
            name=group_name,
            members=members,
            members_file=group_cfg.get("members_file"),
            initial_credit=float(group_cfg.get("initial_credit", policy.initial_credit)),
            min_balance=float(group_cfg.get("min_balance", policy.min_balance)),
        ))

    # Parse skill policies (NEW)
    skill_policies = {}
    for skill_name, skill_cfg in policy_cfg.get("skill", {}).items():
        skill_policies[skill_name.lower()] = SkillPolicy(
            skill_name=skill_name.lower(),
            initial_credit=float(skill_cfg["initial_credit"]) if "initial_credit" in skill_cfg else None,
            min_balance=float(skill_cfg["min_balance"]) if "min_balance" in skill_cfg else None,
        )

    # 4. Create and start node
    node = DHTNode(bind_host, port, storage_path, advertise_host=advertise_host, policy=policy, config=config)
    await node.start()
    
    # Set group and skill policies (NEW)
    node._group_policies = group_policies
    node._skill_policies = skill_policies

    # Ensure node is in its own peer table so storage queries return its own skills [R-01]
    await node._enqueue_write(node.storage.upsert_peer, node.node_info)

    # E-07: Multi-identity startup — parse [identities.*] sections and instantiate
    # If no [identities] section: single-identity backward-compatible mode (no-op)
    from ..cli.config import parse_identity_configs
    from ..dht.identity_storage import setup_identities
    _identity_configs = parse_identity_configs(config)
    if _identity_configs:
        _vault = getattr(node, "_vault", None)
        _base_data_dir = Path(data_dir)
        _setup_identities = setup_identities(
            _identity_configs,
            base_data_dir=_base_data_dir,
            vault=_vault,
            registry=node._identity_registry,
            debug=bool(config.get("node", {}).get("event_bus_debug", False)),
        )
        # E-07: Build skill-to-identity map for TaskRequest demux (D's approach)
        for _ident in _setup_identities:
            for _skill in _ident.skills:
                node._skill_to_identity[_skill] = _ident.node_id
        logger.info(f"MULTI_IDENTITY_STARTUP identities={len(_setup_identities)} skills_mapped={len(node._skill_to_identity)}")
    # Single-identity mode: _identity_registry was initialized in DHTNode.__init__
    # with the node's own node_id as default — no further action needed.

    print(f"Node ID: {node.node_info.node_id}")
    print(f"Listening: {bind_host}:{port}")
    if advertise_host and advertise_host != bind_host:
        print(f"Advertising: {advertise_host}")
    sys.stdout.flush()

    # 5. Pre-load visibility config before join [P6A-003]
    # _reannounce_all() runs during join() and defaults missing visibility to "public".
    # Without this, persisted private skills from a previous session leak on restart.
    for name, skill_cfg in config.get("skills", {}).items():
        visibility = skill_cfg.get("visibility", "public")
        if visibility in ("public", "private", "whitelist"):
            node._skill_visibility[name.lower()] = visibility
            node._skill_allowed_nodes[name.lower()] = skill_cfg.get("allowed_nodes", [])

    # 6. Join network — try cached peers first, then bootstrap
    joined = False
    try:
        joined = await node.reconnect_from_cache()
    except Exception as e:
        logger.debug(f"Peer cache reconnect failed: {e}")

    if not joined and bootstrap_peers:
        try:
            joined = await node.join(bootstrap_peers)
            if joined:
                logger.info(f"Joined network via bootstrap peers")
            else:
                print(f"Warning: could not join any bootstrap peer: {bootstrap_peers}", file=sys.stderr)
        except Exception as e:
            print(f"Could not join network: {e}", file=sys.stderr)
            print(f"  Check that bootstrap peers are running and reachable.", file=sys.stderr)

    # 7. Load skills from config (handlers, announce)
    skills_loaded = await load_skills_from_config(node, config, config_dir)

    # 7a. Register system skills (knarr-mail, etc.)
    await node.register_system_skills(config)

    # 7b. Load per-skill secrets
    node.load_secrets(os.path.join(data_dir, "secrets.toml"))
    if getattr(node, "_generated_identity_certs", False):
        _log_operator_backup_instruction(data_dir)

    # 7c. Refresh node/info meta cache now that skills are loaded
    node.refresh_node_meta()

    # 8. Start bridges
    for cmd_str, timeout in config.get("bridges", {}).items():
        try:
            command = shlex.split(cmd_str)
            await node.start_mcp_bridge(command, tool_timeout=float(timeout))
            print(f"Bridged: {cmd_str}")
        except Exception as e:
            print(f"Failed to start bridge '{cmd_str}': {e}", file=sys.stderr)
            
    for bridge_cmd in args.bridge:
        try:
            command = shlex.split(bridge_cmd)
            await node.start_mcp_bridge(command, tool_timeout=args.bridge_timeout or 30.0)
            print(f"Bridged: {bridge_cmd}")
        except Exception as e:
            print(f"Failed to start bridge '{bridge_cmd}': {e}", file=sys.stderr)

    if skills_loaded > 0:
        print(f"Loaded {skills_loaded} skills from config")
    sys.stdout.flush()

    # Start cockpit if configured
    cockpit_port = args.cockpit if args.cockpit is not None else config.get("cockpit", {}).get("port", 0)
    cockpit_server = None
    if cockpit_port > 0:
        from ..dashboard.server import CockpitServer
        cockpit_bind = config.get("cockpit", {}).get("bind", "127.0.0.1")
        cockpit_token = resolve_cockpit_token(
            data_dir, config.get("cockpit", {}).get("auth_token", "")
        )
        cockpit_exposures = dict(config.get("expose", {}))
        # Merge expose.toml (cockpit-created exposures) if present
        expose_toml_path = os.path.join(config_dir, "expose.toml")
        if os.path.exists(expose_toml_path):
            try:
                import tomllib
                with open(expose_toml_path, "rb") as f:
                    extra = tomllib.load(f)
                for k, v in extra.items():
                    if isinstance(v, dict) and k not in cockpit_exposures:
                        cockpit_exposures[k] = v
            except Exception as e:
                print(f"Warning: failed to load expose.toml: {e}", file=sys.stderr)
        # Cockpit TLS mode: "auto" (default, HTTPS), "off" (HTTP), "both" (HTTP + HTTPS)
        cockpit_tls_mode = config.get("cockpit", {}).get("tls", "auto")
        cockpit_cert, cockpit_key = "", ""
        if cockpit_tls_mode != "off":
            # Use ECDSA cert for cockpit (browser-compatible), not Ed25519
            from ..mail.tls import generate_cockpit_cert, resolve_cockpit_cert_paths
            cockpit_cert, cockpit_key = resolve_cockpit_cert_paths(config, data_dir)
            if not os.path.exists(cockpit_cert) or not os.path.exists(cockpit_key):
                cockpit_cfg = config.get("cockpit", {})
                if "tls_cert" not in cockpit_cfg and "tls_key" not in cockpit_cfg:
                    generate_cockpit_cert(node.node_info.node_id, data_dir)
        cockpit_server = CockpitServer(node, cockpit_bind, cockpit_port, cockpit_token,
                                       exposures=cockpit_exposures, config_dir=config_dir,
                                       cert_path=cockpit_cert, key_path=cockpit_key,
                                       tls_mode=cockpit_tls_mode)
        await cockpit_server.start()
        if cockpit_tls_mode == "both":
            scheme = "http"
            url = f"{scheme}://{cockpit_bind}:{cockpit_server.port}"
        else:
            scheme = "https" if cockpit_tls_mode != "off" else "http"
            url = f"{scheme}://{cockpit_bind}:{cockpit_server.port}"
        if cockpit_tls_mode == "both":
            lines = [f"Cockpit: {url} (HTTP) + https://{cockpit_bind}:{cockpit_server._https_port} (HTTPS)",
                     f"Token:   {cockpit_token}"]
        else:
            lines = [f"Cockpit: {url}", f"Token:   {cockpit_token}"]
        w = max(len(l) for l in lines) + 4
        print(f"\n  +{'-' * w}+")
        for l in lines:
            print(f"  |  {l:<{w - 2}}|")
        print(f"  +{'-' * w}+")
        # Auto-open browser when launched via 'knarr run'
        if getattr(args, '_open_browser', False):
            import webbrowser
            webbrowser.open(url)

    # 9. Wait for shutdown
    shutdown = asyncio.Event()
    node._shutdown_event = shutdown  # upgrade loop can trigger clean shutdown
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except (NotImplementedError, ValueError):
            pass

    # Write PID file for skill install/remove to signal reload
    pid_path = os.path.join(data_dir, "knarr.pid")
    try:
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.warning(f"Could not write PID file: {e}")

    # 10. SIGHUP hot-reload: re-read config, load new skills
    async def reload_skills():
        try:
            fresh_config = load_config(config_path, explicit=bool(args.config))
            fresh_dir = os.path.dirname(os.path.abspath(config_path)) if config_path.exists() else os.getcwd()
            fresh_data_dir, fresh_data_dir_explicit = _resolve_data_dir(getattr(args, "data_dir", None), fresh_config, fresh_dir)
            os.makedirs(fresh_data_dir, exist_ok=True)
            fresh_config["_config_dir"] = fresh_dir
            fresh_config["_data_dir"] = fresh_data_dir
            fresh_config["_data_dir_explicit"] = fresh_data_dir_explicit
            new_count = await load_skills_from_config(node, fresh_config, fresh_dir)
            node.load_secrets(os.path.join(fresh_data_dir, "secrets.toml"))

            # Reload group policies (NEW)
            fresh_policy_cfg = fresh_config.get("policy", {})
            new_groups = []
            for group_name, group_cfg in fresh_policy_cfg.get("group", {}).items():
                members = set(group_cfg.get("members", []))
                members_file = group_cfg.get("members_file")
                if members_file:
                    members_file_path = os.path.join(fresh_dir, members_file) if not os.path.isabs(members_file) else members_file
                    members_file_path = os.path.abspath(members_file_path)
                    fresh_dir_abs = os.path.abspath(fresh_dir)
                    if not members_file_path.startswith(fresh_dir_abs + os.sep) and members_file_path != fresh_dir_abs:
                        logger.warning(f"members_file escapes config directory, skipping: {members_file}")
                    elif os.path.exists(members_file_path):
                        with open(members_file_path) as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    members.add(line)
                new_groups.append(GroupPolicy(
                    name=group_name,
                    members=members,
                    members_file=group_cfg.get("members_file"),
                    initial_credit=float(group_cfg.get("initial_credit", node.policy.initial_credit)),
                    min_balance=float(group_cfg.get("min_balance", node.policy.min_balance)),
                ))
            # Reload skill policies (NEW) — parse into local before committing
            new_skill_policies = {}
            for skill_name, skill_cfg in fresh_policy_cfg.get("skill", {}).items():
                new_skill_policies[skill_name.lower()] = SkillPolicy(
                    skill_name=skill_name.lower(),
                    initial_credit=float(skill_cfg["initial_credit"]) if "initial_credit" in skill_cfg else None,
                    min_balance=float(skill_cfg["min_balance"]) if "min_balance" in skill_cfg else None,
                )

            # Atomic commit: assign both policy sets after all parsing succeeds
            node._group_policies = new_groups
            node._skill_policies = new_skill_policies

            # v0.22.0: Reinitialize GroupEngine with fresh config
            node._config = fresh_config
            node._init_group_engine()
            # Propagate engine + fresh config to groups plugin (mirrors start() pattern)
            config_dir = fresh_config.get("_config_dir", os.getcwd())
            for plugin in node._plugins.plugins:
                if hasattr(plugin, '_load_explicit_groups') and hasattr(plugin, '_group_defs'):
                    # Normalize members_file paths to absolute (config_dir base)
                    groups_cfg = {}
                    for gname, gcfg in fresh_config.get("groups", {}).items():
                        if isinstance(gcfg, dict) and gcfg.get("members_file"):
                            gcfg = dict(gcfg)
                            mf = gcfg["members_file"]
                            if not os.path.isabs(mf):
                                gcfg["members_file"] = os.path.abspath(os.path.join(config_dir, mf))
                        groups_cfg[gname] = gcfg
                    plugin._config["groups"] = groups_cfg
                    # Also merge old-format groups
                    for gname, gcfg in fresh_config.get("policy", {}).get("group", {}).items():
                        if gname not in plugin._config.get("groups", {}):
                            mf = gcfg.get("members_file")
                            if mf and not os.path.isabs(mf):
                                mf = os.path.abspath(os.path.join(config_dir, mf))
                            plugin._config.setdefault("groups", {})[gname] = {
                                "type": "explicit",
                                "members": list(gcfg.get("members", [])),
                                "members_file": mf,
                            }
                    plugin._group_defs.clear()
                    plugin._cache.clear()
                    plugin._load_explicit_groups()
                    # Reload computed group defs
                    for gname, gcfg in plugin._config.get("groups", {}).items():
                        if isinstance(gcfg, dict) and gcfg.get("type") == "computed":
                            plugin._group_defs[gname] = gcfg
                    ctx = getattr(plugin, '_ctx', None)
                    if ctx is not None:
                        node._group_engine = ctx.group_engine

            if new_count > 0:
                print(f"Reload: loaded {new_count} new skills", file=sys.stderr)
            else:
                print(f"Reload: no new skills found", file=sys.stderr)
            if new_groups:
                print(f"Reload: {len(new_groups)} group policies loaded", file=sys.stderr)
        except Exception as e:
            print(f"Reload failed: {e}", file=sys.stderr)

    def sighup_handler():
        loop.create_task(reload_skills())

    if hasattr(signal, 'SIGHUP'):
        try:
            loop.add_signal_handler(signal.SIGHUP, sighup_handler)
        except (NotImplementedError, ValueError, OSError):
            pass

    # Cross-platform reload: watch for sentinel file (works on Windows too)
    reload_sentinel = os.path.join(config_dir, "knarr.reload")
    async def _watch_reload_sentinel():
        while not shutdown.is_set():
            await asyncio.sleep(2)
            if os.path.exists(reload_sentinel):
                try:
                    os.remove(reload_sentinel)
                except OSError:
                    pass
                await reload_skills()
    loop.create_task(_watch_reload_sentinel())

    try:
        await shutdown.wait()
    except asyncio.CancelledError:
        pass
        
    print("\nShutting down...", file=sys.stderr)
    if cockpit_server:
        await cockpit_server.stop()
    # E-07: Close per-identity storage connections before node stop
    for _ident in node._identity_registry.all:
        try:
            if _ident.storage is not None and hasattr(_ident.storage, "close"):
                _ident.storage.close()
        except Exception as _e:
            logger.debug(f"IDENTITY_STORAGE_CLOSE_FAIL name={_ident.name}: {_e}")
    await node.stop()
    try:
        if os.path.exists(pid_path):
            os.unlink(pid_path)
    except OSError:
        pass

    # Auto-upgrade: restart if requested
    if getattr(node, "_restart_requested", False):
        print("Restarting after auto-upgrade...", file=sys.stderr)
        os.execv(sys.executable, [sys.executable] + sys.argv)

async def cmd_upgrade(args):
    """Check for and install updates.

    DEPRECATED — use knarr-watchman upgrade instead.
    """
    from ..dht.upgrade import get_latest_version
    from knarr import __version__

    print("WARNING: 'knarr upgrade' is deprecated as of v0.45.0.", file=sys.stderr)
    print("Use 'knarr-watchman upgrade' for staged upgrades with drain and rollback.", file=sys.stderr)
    print("See: contrib/watchman.toml.example", file=sys.stderr)
    print(file=sys.stderr)

    if args.check:
        # --check is still useful for version display; keep it working
        print(f"Current version:   v{__version__}")
        latest = get_latest_version()
        if latest:
            print(f"Latest available: v{latest}")
            if latest == __version__:
                print("Status: Already up to date.")
            else:
                print(f"Status: Update available — run 'knarr-watchman upgrade' to install.")
        else:
            print("Could not fetch latest version from GitHub.", file=sys.stderr)
        return

    print(f"Current version:   v{__version__}")
    latest = get_latest_version()
    if latest:
        print(f"Latest available: v{latest}")
    sys.exit(1)  # upgrade execution disabled — use knarr-watchman upgrade

    # Newer version available
    config_path = Path(args.config) if args.config else Path("knarr.toml")
    config = load_config(config_path, explicit=bool(args.config))
    config_dir = os.path.dirname(os.path.abspath(config_path)) if config_path.exists() else os.getcwd()
    data_dir, _ = _resolve_data_dir(None, config, config_dir)
    
    print(f"\nUpgrading to v{latest}...")
    
    # 1. Backup
    backup_dir = backup_config(config_dir, __version__, data_dir=data_dir)
    if not backup_dir:
        print("Error: Backup failed. Aborting upgrade for safety.", file=sys.stderr)
        sys.exit(1)
        
    # 2. Install
    if check_and_upgrade(latest):
        # 3. Verify
        if verify_installation(latest):
            print(f"\nSuccessfully upgraded to v{latest}!")
            print("Restart your node to apply changes.")
        else:
            print("\nError: Installation verification failed. Rolling back...", file=sys.stderr)
            rollback_installation(backup_dir, config_dir, data_dir=data_dir)
            sys.exit(1)
    else:
        print("\nError: Upgrade failed. Rolling back...", file=sys.stderr)
        rollback_installation(backup_dir, config_dir, data_dir=data_dir)
        sys.exit(1)


async def cmd_query(args):
    logging.basicConfig(level=getattr(logging, args.log_level or "WARNING"), stream=sys.stderr)

    node = DHTNode("127.0.0.1", args.port or 0)
    await node.start()

    # Ensure node is in its own peer table [R-01]
    await node._enqueue_write(node.storage.upsert_peer, node.node_info)

    peers = [p.strip() for p in args.bootstrap.split(",") if p.strip()]
    try:
        await node.join(peers)
    except Exception as e:
        print(f"Warning: Join failed: {e}", file=sys.stderr)

    if getattr(args, 'all', False):
        query_type = "all"
        value = "*"
    elif args.name:
        query_type = "name"
        value = args.name
    else:
        query_type = "tag"
        value = args.tag
    results = await node.query(query_type, value, network_timeout=args.timeout or 5.0)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("No skills found.")
        else:
            print(f"{'SKILL':<20} {'PROVIDER':<20} {'HOST':<20} {'SIDECAR':<8} {'DESCRIPTION'}")
            for r in results:
                skill = r["skill_sheet"]
                node_id_short = r["node_id"][:16] + "..."
                host_port = f"{r['host']}:{r['port']}"
                sidecar_port = r.get("sidecar_port", 0)
                sidecar_str = str(sidecar_port) if sidecar_port > 0 else "-"
                print(f"{skill['name']:<20} {node_id_short:<20} {host_port:<20} {sidecar_str:<8} {skill.get('description', '')[:40]}")

    await node.stop()

def cmd_address(args):
    """Manage address book entries."""
    storage = Storage(args.storage or "node.db")
    
    if args.addr_command == "list":
        explicit = storage.get_addresses_by_tier("explicit")
        cached = storage.get_addresses_by_tier("cached")
        
        if explicit:
            print("Explicit Entries:")
            print(f"  {'LABEL':<20} {'NODE_ID':<20} {'LAST_IP':<20} {'PORT':<6}")
            for e in explicit:
                print(f"  {e.get('label') or '-':<20} {e['node_id'][:16]+'...':<20} {e.get('last_ip') or '-':<20} {e.get('last_port') or '-'}")
        
        if cached:
            if explicit: print("")
            print("Cached Entries:")
            print(f"  {'NODE_ID':<20} {'LAST_IP':<20} {'PORT':<6} {'LAST_SEEN'}")
            for e in cached:
                from datetime import datetime
                ls = datetime.fromtimestamp(e['last_seen']).strftime("%Y-%m-%d %H:%M") if e.get('last_seen') else "-"
                print(f"  {e['node_id'][:16]+'...':<20} {e.get('last_ip') or '-':<20} {e.get('last_port') or '-':<6} {ls}")
                
        if not explicit and not cached:
            print("Address book is empty.")

    elif args.addr_command == "add":
        storage.upsert_address(args.node_id, tier="explicit", label=args.label)
        print(f"Added address entry for {args.node_id[:16]}...")

    elif args.addr_command == "remove":
        # Delete from all tiers for this node_id
        conn = storage._get_conn()
        conn.execute("DELETE FROM address_book WHERE node_id = ?", (args.node_id,))
        conn.commit()
        print(f"Removed address entry for {args.node_id[:16]}...")

    storage.close()

async def cmd_request(args):
    logging.basicConfig(level=getattr(logging, args.log_level or "WARNING"), stream=sys.stderr)
    
    try:
        input_data = json.loads(args.input_json)
        if not isinstance(input_data, dict):
            print(f"Invalid --input: must be a JSON object (e.g. '{{\"text\": \"hello\"}}'), not a {type(input_data).__name__}.", file=sys.stderr)
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in --input: {e}. Input must be a valid JSON object, e.g. '{{\"text\": \"hello\"}}'", file=sys.stderr)
        sys.exit(1)
        
    if not args.direct and not args.bootstrap:
        print("Error: --bootstrap or --direct is required.", file=sys.stderr)
        sys.exit(1)

    node = DHTNode("127.0.0.1", args.port or 0, ephemeral=True)
    await node.start()
    await node._enqueue_write(node.storage.upsert_peer, node.node_info)

    try:
        # Direct mode: skip DHT, send straight to host:port
        if args.direct:
            if ":" not in args.direct:
                print("Error: --direct requires host:port format (e.g. localhost:9010)", file=sys.stderr)
                sys.exit(1)
            host, port_str = args.direct.rsplit(":", 1)
            try:
                direct_port = int(port_str)
            except ValueError:
                print(f"Error: invalid port in --direct: {port_str}", file=sys.stderr)
                sys.exit(1)
            providers = [{"node_id": "", "host": host, "port": direct_port,
                          "sidecar_port": direct_port + 1, "skill_sheet": {"price": 1.0}}]
        else:
            peers = [p.strip() for p in args.bootstrap.split(",") if p.strip()]
            joined = await node.join(peers)
            if not joined:
                print(f"Could not join network via {args.bootstrap} — peer may be offline or unreachable.", file=sys.stderr)
                sys.exit(1)

            # Discover provider
            results = await node.query("name", args.skill, network_timeout=args.timeout or 30.0)
            if not results:
                print(f"No providers found for skill '{args.skill}'. Check that a provider is running and has announced this skill.", file=sys.stderr)
                sys.exit(1)
            providers = results

        last_error = None
        for provider in providers:
            skill_price = provider["skill_sheet"].get("price", 1.0)
            
            # Process @file references in input
            sidecar_port = provider.get("sidecar_port", 0)
            if sidecar_port > 0:
                for key, value in input_data.items():
                    if isinstance(value, str) and value.startswith("@"):
                        file_path = value[1:]
                        if not os.path.exists(file_path):
                            print(f"Error: File not found: {file_path}", file=sys.stderr)
                            sys.exit(1)
                        # Upload to provider's sidecar
                        try:
                            with open(file_path, "rb") as f:
                                file_bytes = f.read()
                            hash = await upload_asset(provider["host"], sidecar_port, file_bytes, node._signing_key)
                            input_data[key] = f"knarr-asset://{hash}"
                        except Exception as e:
                            print(f"Error uploading asset {file_path}: {e}", file=sys.stderr)
                            sys.exit(1)
            
            # Request task
            res = await node.request_task(
                provider["node_id"], provider["host"], provider["port"],
                args.skill, input_data, timeout_ms=int((args.timeout or 30.0) * 1000),
                skill_price=skill_price
            )
            
            if res.status == "completed":
                if args.output_dir:
                    os.makedirs(args.output_dir, exist_ok=True)
                    sidecar_port = provider.get("sidecar_port", 0)
                    if sidecar_port > 0:
                        for key, value in res.output_data.items():
                            if not isinstance(value, str):
                                continue
                            # Accept both "knarr-asset://<hash>" and bare 64-char hex hashes
                            asset_hash = None
                            if value.startswith("knarr-asset://"):
                                asset_hash = value[len("knarr-asset://"):]
                            elif len(value) == 64 and all(c in '0123456789abcdef' for c in value):
                                asset_hash = value
                            if not asset_hash:
                                continue
                            try:
                                file_bytes = await download_asset(provider["host"], sidecar_port, asset_hash, node._signing_key)
                                output_path = os.path.join(args.output_dir, f"{key}_{asset_hash[:12]}")
                                with open(output_path, "wb") as f:
                                    f.write(file_bytes)
                                print(f"Downloaded: {key} -> {output_path} ({len(file_bytes)} bytes)")
                            except Exception as e:
                                print(f"Error downloading asset {key}: {e}", file=sys.stderr)

                if args.json:
                    print(json.dumps(res.to_dict(), indent=2))
                else:
                    print(f"Status: {res.status}")
                    print("Output:")
                    for k, v in res.output_data.items():
                        print(f"  {k}: {v}")
                return

            err = res.error or {}
            err_code = err.get("code", "")

            if err_code == "RETRY_AFTER":
                wait_s = min(err.get("retry_after_seconds", 5), 30)
                print(f"Provider busy, retrying in {wait_s}s...", file=sys.stderr)
                await asyncio.sleep(wait_s)
                # Retry same provider
                res = await node.request_task(
                    provider["node_id"], provider["host"], provider["port"],
                    args.skill, input_data, timeout_ms=int((args.timeout or 30.0) * 1000),
                    skill_price=skill_price
                )
                if res.status == "completed":
                    if args.json:
                        print(json.dumps(res.to_dict(), indent=2))
                    else:
                        print(f"Status: {res.status}")
                        print("Output:")
                        for k, v in res.output_data.items():
                            print(f"  {k}: {v}")
                    return
                last_error = res

            elif err_code == "PROVIDER_BUSY":
                print(f"Provider {provider['node_id'][:16]}... busy, trying next...", file=sys.stderr)
                last_error = res
                continue  # Try next provider

            else:
                # Other error: don't try more providers
                last_error = res
                break

        # All providers failed
        if last_error:
            if args.json:
                print(json.dumps(last_error.to_dict(), indent=2))
            else:
                err = last_error.error or {}
                print(f"Status: {last_error.status}")
                print(f"Error: {err.get('code', 'UNKNOWN')} — {err.get('message', 'No detail')}")
                
    finally:
        await node.stop()

def cmd_demand(args):
    storage = Storage(args.storage or "node.db")
    demand = storage.get_demand()
    if args.json:
        print(json.dumps(demand, indent=2))
    else:
        if not demand:
            print("No demand recorded.")
        else:
            print(f"{'QUERY':<30} {'TYPE':<10} {'COUNT':<8} {'LAST QUERIED'}")
            for d in demand:
                from datetime import datetime
                ts = datetime.fromtimestamp(d["last_queried"]).strftime("%Y-%m-%d %H:%M")
                print(f"{d['value']:<30} {d['type']:<10} {d['count']:<8} {ts}")
    storage.close()

def cmd_info(args):
    storage = Storage(args.storage or "node.db")
    key = storage.get_node_key()
    if key:
        signing_key = SigningKey(key)
        public_key = signing_key.verify_key.encode()
        node_id = hashlib.sha256(public_key).hexdigest()
        print(f"Node ID: {node_id}")
        print(f"Public Key: {public_key.hex()}")
    else:
        print("No node identity found (new node)")

    if getattr(args, 'reputation', False):
        # Counterparty count
        count = storage.get_counterparty_count()
        print(f"\nCounterparties: {count} unique peers")

        # Provider history
        reputations = storage.get_all_provider_reputations()
        if reputations:
            print(f"\nProvider History (last 30 days):")
            print(f"  {'Provider':<20} {'Success':>8}  {'Avg Time':>10}  {'Tasks':>6}")
            for rep in reputations:
                node_prefix = rep["provider_node_id"][:16] + "..."
                sr = f"{rep['success_rate']*100:.1f}%" if rep["success_rate"] is not None else "--"
                avg = f"{rep['avg_wall_time_ms']:.0f}ms" if rep["avg_wall_time_ms"] is not None else "--"
                print(f"  {node_prefix:<20} {sr:>8}  {avg:>10}  {rep['total_tasks']:>6}")
        else:
            print("\nNo provider history recorded.")

        # Ledger summary
        entries = storage.get_all_ledger_entries()
        if entries:
            print(f"\nLedger:")
            print(f"  {'Peer Key':<20} {'Balance':>10}  {'Provided':>9}  {'Consumed':>9}")
            for e in entries:
                key_prefix = e["peer_public_key"][:16] + "..."
                print(f"  {key_prefix:<20} {e['balance']:>10.1f}  {e['tasks_provided']:>9}  {e['tasks_consumed']:>9}")

    storage.close()

def main():
    from knarr import __version__
    parser = argparse.ArgumentParser(prog="knarr", description="Knarr P2P network node")
    parser.add_argument("--version", action="version", version=f"knarr {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # run (default — auto-init + serve + open browser)
    run_parser = subparsers.add_parser("run", help="Quick start: auto-init, serve, and open cockpit")
    run_parser.add_argument("--port", type=int, default=None)
    run_parser.add_argument("--host", default=None)
    run_parser.add_argument("--advertise-host", default=None)
    run_parser.add_argument("--storage", default=None)
    run_parser.add_argument("--bootstrap", default=None)
    run_parser.add_argument("--config", default=None)
    run_parser.add_argument("--bridge", action="append", default=[])
    run_parser.add_argument("--bridge-timeout", type=float, default=None)
    run_parser.add_argument("--cockpit", type=int, default=None)
    run_parser.add_argument("--log-level", default=None)

    # init
    init_parser = subparsers.add_parser("init", help="Create a new Knarr provider project")
    init_parser.add_argument("directory", help="Project directory to create")
    init_parser.add_argument("--port", type=int, default=9000)
    init_parser.add_argument("--bootstrap", default="bootstrap1.knarr.network:9000")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start a Knarr node")
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--advertise-host", default=None)
    serve_parser.add_argument("--storage", default=None)
    serve_parser.add_argument("--data-dir", default=None)
    serve_parser.add_argument("--bootstrap", default=None)
    serve_parser.add_argument("--config", default=None)
    serve_parser.add_argument("--bridge", action="append", default=[])
    serve_parser.add_argument("--bridge-timeout", type=float, default=None)
    serve_parser.add_argument("--cockpit", type=int, default=None, help="Cockpit dashboard port (0=disabled)")
    serve_parser.add_argument("--log-level", default=None)

    # query
    query_parser = subparsers.add_parser("query", help="Query the network for skills")
    query_parser.add_argument("--bootstrap", required=True)
    group = query_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name")
    group.add_argument("--tag")
    group.add_argument("--all", action="store_true")
    query_parser.add_argument("--port", type=int, default=None)
    query_parser.add_argument("--timeout", type=float, default=None)
    query_parser.add_argument("--json", action="store_true")
    query_parser.add_argument("--log-level", default=None)

    # request
    request_parser = subparsers.add_parser("request", help="Execute a task on the network")
    request_parser.add_argument("--skill", required=True)
    request_parser.add_argument("--input", required=True, dest="input_json")
    request_parser.add_argument("--bootstrap", default=None)
    request_parser.add_argument("--direct", default=None, help="Send directly to host:port, bypass DHT")
    request_parser.add_argument("--port", type=int, default=None)
    request_parser.add_argument("--timeout", type=float, default=None)
    request_parser.add_argument("--output-dir", default=None)
    request_parser.add_argument("--json", action="store_true")
    request_parser.add_argument("--log-level", default=None)

    # demand
    demand_parser = subparsers.add_parser("demand", help="Show unmet skill demand")
    demand_parser.add_argument("--storage", default=None)
    demand_parser.add_argument("--json", action="store_true")

    # info
    info_parser = subparsers.add_parser("info", help="Show node identity and reputation data")
    info_parser.add_argument("--storage", default=None)
    info_parser.add_argument("--reputation", action="store_true", help="Show reputation data for known providers")

    # address
    address_parser = subparsers.add_parser("address", help="Manage address book entries")
    address_subparsers = address_parser.add_subparsers(dest="addr_command", required=True)
    address_list_parser = address_subparsers.add_parser("list", help="List address entries")
    address_list_parser.add_argument("--storage", default=None)
    address_add_parser = address_subparsers.add_parser("add", help="Add explicit address entry")
    address_add_parser.add_argument("node_id", help="Node ID to add")
    address_add_parser.add_argument("--label", help="Optional label")
    address_add_parser.add_argument("--storage", default=None)
    address_rm_parser = address_subparsers.add_parser("remove", help="Remove address entry")
    address_rm_parser.add_argument("node_id", help="Node ID to remove")
    address_rm_parser.add_argument("--storage", default=None)

    # upgrade
    upgrade_parser = subparsers.add_parser("upgrade", help="Check for and install updates")
    upgrade_parser.add_argument("--check", action="store_true", help="Check only, don't install")
    upgrade_parser.add_argument("--config", default=None, help="Path to knarr.toml")

    # tls
    tls_parser = subparsers.add_parser("tls", help="TLS certificate management")
    tls_subparsers = tls_parser.add_subparsers(dest="tls_command", required=True)
    tls_init_parser = tls_subparsers.add_parser("init", help="Generate TLS certificate from node identity")
    tls_init_parser.add_argument("--config-dir", default=None, help="Config directory (default: cwd)")
    tls_init_parser.add_argument("--days", type=int, default=365, help="Certificate validity in days")
    tls_init_parser.add_argument("--force", action="store_true", help="Overwrite existing cert/key files")
    tls_init_parser.add_argument("--storage", default=None, help="Storage database path")

    # skill (subcommand group)
    skill_parser = subparsers.add_parser("skill", help="Manage skill packages")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)

    # skill init
    skill_init_parser = skill_subparsers.add_parser("init", help="Create a new skill package")
    skill_init_parser.add_argument("name", help="Skill name (lowercase, hyphens ok)")

    # skill install
    skill_install_parser = skill_subparsers.add_parser("install", help="Install a skill")
    skill_install_parser.add_argument("source", help="Local dir, .knarr file, or git+https://... URL")
    skill_install_parser.add_argument("--force", action="store_true", help="Overwrite existing skill")
    skill_install_parser.add_argument("--upgrade", action="store_true", help="Upgrade (preserve data_dir)")

    # skill remove
    skill_remove_parser = skill_subparsers.add_parser("remove", help="Remove an installed skill")
    skill_remove_parser.add_argument("name", help="Skill name to remove")
    skill_remove_parser.add_argument("--purge", action="store_true", help="Also delete data directory")

    # skill list
    skill_list_parser = skill_subparsers.add_parser("list", help="List installed skills")
    skill_list_parser.add_argument("--json", action="store_true")

    # skill pack
    skill_pack_parser = skill_subparsers.add_parser("pack", help="Create .knarr archive from directory")
    skill_pack_parser.add_argument("directory", help="Skill directory to pack")

    # skill export
    skill_export_parser = skill_subparsers.add_parser("export", help="Export installed skill as .knarr")
    skill_export_parser.add_argument("name", help="Skill name to export")
    skill_export_parser.add_argument("--bundle", action="store_true", help="Include dependencies")

    # group subcommand
    group_parser = subparsers.add_parser("group", help="Manage node groups")
    group_parser.add_argument("--json", action="store_true", help="Output as JSON")
    group_subparsers = group_parser.add_subparsers(dest="group_command", required=True)
    group_subparsers.add_parser("list", help="List all groups with member counts")
    group_members_parser = group_subparsers.add_parser("members", help="List members of a group")
    group_members_parser.add_argument("name", help="Group name")
    group_add_parser = group_subparsers.add_parser("add", help="Add node to explicit group")
    group_add_parser.add_argument("name", help="Group name")
    group_add_parser.add_argument("node_id", help="Node ID to add")
    group_remove_parser = group_subparsers.add_parser("remove", help="Remove node from explicit group")
    group_remove_parser.add_argument("name", help="Group name")
    group_remove_parser.add_argument("node_id", help="Node ID to remove")
    group_refresh_parser = group_subparsers.add_parser("refresh", help="Force group refresh")
    group_refresh_parser.add_argument("name", nargs="?", help="Specific group (omit for all)")

    args = parser.parse_args()

    # Default to "run" when no subcommand given
    if args.command is None:
        args = run_parser.parse_args([])
        args.command = "run"

    try:
        if args.command == "run":
            asyncio.run(cmd_run(args))
        elif args.command == "init":
            print(init_project(args.directory, args.port, args.bootstrap))
        elif args.command == "serve":
            asyncio.run(cmd_serve(args))
        elif args.command == "query":
            asyncio.run(cmd_query(args))
        elif args.command == "request":
            asyncio.run(cmd_request(args))
        elif args.command == "demand":
            cmd_demand(args)
        elif args.command == "info":
            cmd_info(args)
        elif args.command == "address":
            cmd_address(args)
        elif args.command == "upgrade":
            asyncio.run(cmd_upgrade(args))
        elif args.command == "tls":
            if args.tls_command == "init":
                config_dir = args.config_dir or os.getcwd()
                storage_path = args.storage or os.path.join(config_dir, "node.db")
                storage = Storage(storage_path)
                key_bytes = storage.get_node_key()
                if not key_bytes:
                    print("Error: No node identity found. Run 'knarr init' first.", file=sys.stderr)
                    sys.exit(1)
                signing_key = SigningKey(key_bytes)
                node_id = hashlib.sha256(signing_key.verify_key.encode()).hexdigest()
                from ..mail.tls import generate_tls_cert, generate_cockpit_cert
                cert_path, key_path = generate_tls_cert(
                    key_bytes, node_id, config_dir,
                    days=args.days, force=args.force
                )
                print(f"Protocol cert (Ed25519): {cert_path}")
                print(f"Protocol key:            {key_path}")
                cockpit_cert, cockpit_key = generate_cockpit_cert(
                    node_id, config_dir,
                    days=args.days, force=args.force
                )
                print(f"Cockpit cert (ECDSA):    {cockpit_cert}")
                print(f"Cockpit key:             {cockpit_key}")
                print(f"Valid for {args.days} days")
                storage.close()
        elif args.command == "skill":
            from .skill import (cmd_skill_init, cmd_skill_install, cmd_skill_remove,
                                cmd_skill_list, cmd_skill_pack, cmd_skill_export)

            config_dir = os.getcwd()  # default — skill commands operate relative to cwd
            config_arg = getattr(args, "config", None)
            if config_arg:
                config_dir = os.path.dirname(os.path.abspath(config_arg))

            if args.skill_command == "init":
                print(cmd_skill_init(args.name))
            elif args.skill_command == "install":
                result = cmd_skill_install(args.source, config_dir, force=args.force, upgrade=args.upgrade)
                print(result)
            elif args.skill_command == "remove":
                print(cmd_skill_remove(args.name, config_dir, purge=args.purge))
            elif args.skill_command == "list":
                print(cmd_skill_list(config_dir, json_output=args.json))
            elif args.skill_command == "pack":
                print(cmd_skill_pack(args.directory))
            elif args.skill_command == "export":
                print(cmd_skill_export(args.name, config_dir, bundle=args.bundle))
        elif args.command == "group":
            from .group import cmd_group
            cmd_group(args)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
