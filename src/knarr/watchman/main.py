"""knarr-watchman CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from .config import load_config
from .supervisor import Supervisor


def _setup_logging(data_dir: str, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_path = os.path.join(data_dir, "watchman", "watchman.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def _write_pid(data_dir: str) -> None:
    pid_path = os.path.join(data_dir, "watchman", "watchman.pid")
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid(data_dir: str) -> None:
    pid_path = os.path.join(data_dir, "watchman", "watchman.pid")
    try:
        os.remove(pid_path)
    except FileNotFoundError:
        pass


def _read_pid(data_dir: str) -> int | None:
    pid_path = os.path.join(data_dir, "watchman", "watchman.pid")
    try:
        with open(pid_path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is alive, False if confirmed dead.

    On Windows, os.kill(pid, 0) raises OSError even for live processes when
    the caller lacks PROCESS_QUERY_INFORMATION rights.  Use OpenProcess with
    PROCESS_QUERY_LIMITED_INFORMATION (0x1000) instead:
      - non-NULL handle  → alive (close it and return True)
      - NULL + ERROR_ACCESS_DENIED (5) → alive but restricted (return True)
      - NULL + any other error → dead or no such process (return False)
    """
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED
    else:
        import errno
        try:
            os.kill(pid, 0)
            return True
        except OSError as e:
            if e.errno == errno.EPERM:
                return True  # alive but we lack rights
            return False
        except ProcessLookupError:
            return False


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    """Run the supervisor (foreground)."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["node"].get("data_dir") or os.path.join(os.getcwd(), ".node")
    cfg["node"]["data_dir"] = data_dir

    _setup_logging(data_dir, args.verbose)
    _write_pid(data_dir)

    log = logging.getLogger("knarr.watchman")
    log.info("WATCHMAN_INIT data_dir=%s config=%s", data_dir, args.config)

    supervisor = Supervisor(cfg, config_path=args.config)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            import signal as _signal
            loop.add_signal_handler(_signal.SIGTERM, lambda: asyncio.create_task(supervisor.stop()))
            loop.add_signal_handler(_signal.SIGHUP, lambda: supervisor.reload_config(args.config))
        try:
            await supervisor.run()
        finally:
            _remove_pid(data_dir)
            log.info("WATCHMAN_EXIT")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


def cmd_status(args: argparse.Namespace) -> None:
    """Print watchman + node status."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["node"].get("data_dir") or os.path.join(os.getcwd(), ".node")

    pid = _read_pid(data_dir)
    cockpit_url = cfg["health"]["cockpit_url"]

    # Validate PID is actually alive (stale PID files on Windows after SIGTERM)
    if pid is not None and not _is_pid_alive(pid):
        _remove_pid(data_dir)
        pid = None

    print(f"watchman pid: {pid or 'not running'}")

    # Try to fetch /health from the node
    import urllib.request
    health_url = f"{cockpit_url}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=3) as resp:
            data = json.loads(resp.read())
            print(f"node: running")
            print(f"  version:    {data.get('version', '?')}")
            print(f"  peers:      {data.get('peer_count', '?')}")
            print(f"  uptime:     {_fmt_uptime(data.get('uptime', 0))}")
            print(f"  health:     ok")
    except Exception as e:
        print(f"node: unreachable ({e})")


def cmd_stop(args: argparse.Namespace) -> None:
    """Send SIGTERM to running watchman."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["node"].get("data_dir") or os.path.join(os.getcwd(), ".node")

    pid = _read_pid(data_dir)
    if not pid:
        print("watchman: not running (no pid file)")
        sys.exit(1)

    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to watchman (PID {pid})")
        # On Windows, SIGTERM = TerminateProcess (abrupt). Clean up PID file here
        # since the finally: block in cmd_run may not execute.
        if sys.platform == "win32":
            _remove_pid(data_dir)
    except ProcessLookupError:
        print(f"watchman (PID {pid}) not found — stale pid file?")
        _remove_pid(data_dir)
        sys.exit(1)


def cmd_upgrade(args: argparse.Namespace) -> None:
    """Trigger a staged upgrade (manual)."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["node"].get("data_dir") or os.path.join(os.getcwd(), ".node")
    cfg["node"]["data_dir"] = data_dir
    _setup_logging(data_dir, args.verbose)

    from .supervisor import Supervisor
    from .upgrader import Upgrader

    # Minimal supervisor stub for drain + spawn during manual upgrade
    supervisor = Supervisor(cfg)

    async def _run() -> None:
        upgrader = Upgrader(cfg, supervisor)
        tag = getattr(args, "tag", None) or None
        success = await upgrader.run_upgrade(tag=tag)
        if success:
            print("Upgrade complete.")
        else:
            print("Upgrade failed — rolled back.")
            sys.exit(1)

    asyncio.run(_run())


def cmd_rollback(args: argparse.Namespace) -> None:
    """Roll back to the previous version recorded in backup/."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["node"].get("data_dir") or os.path.join(os.getcwd(), ".node")
    cfg["node"]["data_dir"] = data_dir
    _setup_logging(data_dir, args.verbose)

    from .upgrader import _parse_source, _install_from_tag

    backup_root = os.path.join(data_dir, "watchman", "backup")
    if not os.path.isdir(backup_root):
        print("No backup found.")
        sys.exit(1)

    versions = sorted(os.listdir(backup_root))
    if not versions:
        print("No backup found.")
        sys.exit(1)

    target = versions[-1]
    version_file = os.path.join(backup_root, target, "version.txt")
    if os.path.exists(version_file):
        with open(version_file) as f:
            target = f.read().strip()

    source = cfg["upgrade"]["source"]
    org, repo = _parse_source(source)
    print(f"Rolling back to v{target}...")
    _install_from_tag(org, repo, f"v{target}")
    print(f"Rolled back to v{target}. Restart knarr-watchman to apply.")


def cmd_init(args: argparse.Namespace) -> None:
    """Bootstrap a new knarr node from scratch."""
    data_dir = args.data_dir
    if not data_dir:
        print("Error: --data-dir is required for init", file=sys.stderr)
        sys.exit(1)

    from .init import run_init

    try:
        run_init(
            data_dir=data_dir,
            force=args.force,
            enable_thrall=args.enable_thrall,
            github_token=args.github_token,
            wheel=args.wheel,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_plugin(args: argparse.Namespace) -> None:
    """Dispatch plugin subcommands."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["node"].get("data_dir") or os.path.join(os.getcwd(), ".node")
    cfg["node"]["data_dir"] = data_dir

    from .plugin_manager import PluginManager
    pm = PluginManager(cfg)

    if args.plugin_cmd == "install":
        pm.install(args.name)
        print(f"Plugin '{args.name}' installed.")
    elif args.plugin_cmd == "remove":
        pm.remove(args.name)
        print(f"Plugin '{args.name}' removed.")
    elif args.plugin_cmd == "enable":
        pm.enable(args.name)
        print(f"Plugin '{args.name}' enabled.")
    elif args.plugin_cmd == "disable":
        pm.disable(args.name)
        print(f"Plugin '{args.name}' disabled.")
    elif args.plugin_cmd == "list":
        plugins = pm.list_plugins()
        if not plugins:
            print("No plugins declared in manifest.")
        else:
            print(f"{'NAME':<20} {'SOURCE':<40} {'ENABLED':<8} {'VERSION'}")
            print("-" * 78)
            for p in plugins:
                enabled = "yes" if p["enabled"] else "no"
                print(f"{p['name']:<20} {p['source']:<40} {enabled:<8} {p['installed_version']}")
    elif args.plugin_cmd == "sync":
        pm.sync()
        print("Plugin sync complete.")


def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knarr-watchman",
        description="Knarr node process supervisor",
    )
    parser.add_argument(
        "--config", default="watchman.toml",
        help="Path to watchman.toml (default: watchman.toml in cwd)",
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Node data directory (overrides watchman.toml [node] data_dir)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    sub = parser.add_subparsers(dest="command")
    sub.required = True

    # --- init ---
    init_p = sub.add_parser("init", help="Bootstrap a new knarr node")
    init_p.add_argument("--data-dir", default=None,
                        help="Target data directory (required for init)")
    init_p.add_argument("--force", action="store_true",
                        help="Regenerate identity and vault (does not touch configs)")
    init_p.add_argument("--github-token", default=None,
                        help="GitHub API token for authenticated requests")
    init_p.add_argument("--wheel", default=None,
                        help="Install knarr from local .whl (air-gapped)")
    thrall_group = init_p.add_mutually_exclusive_group()
    thrall_group.add_argument("--enable-thrall", action="store_const",
                              const=True, dest="enable_thrall",
                              help="Enable Thrall without prompting")
    thrall_group.add_argument("--no-thrall", action="store_const",
                              const=False, dest="enable_thrall",
                              help="Disable Thrall without prompting")
    init_p.set_defaults(func=cmd_init, enable_thrall=None)

    run_p = sub.add_parser("run", help="Start watchman supervisor (foreground)")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="Show node and watchman status")
    status_p.set_defaults(func=cmd_status)

    stop_p = sub.add_parser("stop", help="Stop running watchman")
    stop_p.set_defaults(func=cmd_stop)

    upgrade_p = sub.add_parser("upgrade", help="Trigger staged upgrade (manual)")
    upgrade_p.add_argument("--tag", default=None, help="Specific version tag (e.g. v0.45.0)")
    upgrade_p.set_defaults(func=cmd_upgrade)

    rollback_p = sub.add_parser("rollback", help="Roll back to previous version")
    rollback_p.set_defaults(func=cmd_rollback)

    plugin_p = sub.add_parser("plugin", help="Manage plugins from plugins.toml manifest")
    plugin_p.set_defaults(func=cmd_plugin)
    plugin_sub = plugin_p.add_subparsers(dest="plugin_cmd")
    plugin_sub.required = True

    pi = plugin_sub.add_parser("install", help="Install a plugin declared in manifest")
    pi.add_argument("name", help="Plugin name")

    pr = plugin_sub.add_parser("remove", help="Remove an installed plugin")
    pr.add_argument("name", help="Plugin name")

    pe = plugin_sub.add_parser("enable", help="Enable a plugin in the manifest")
    pe.add_argument("name", help="Plugin name")

    pd = plugin_sub.add_parser("disable", help="Disable a plugin in the manifest")
    pd.add_argument("name", help="Plugin name")

    plugin_sub.add_parser("list", help="List plugins and their status")
    plugin_sub.add_parser("sync", help="Sync all enabled plugins from manifest")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
