import logging
import time
from typing import List, Optional, Any

from knarr.core.messages import Message
from knarr.core.models import NodeInfo
from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth

try:
    import miniupnpc
except ImportError:
    miniupnpc = None


class UPnPPlugin(PluginHooks):
    """UPnP port mapping plugin — discovers gateway and maps ports on startup."""

    def __init__(self, ctx: PluginContext, config: dict):
        self._ctx = ctx
        self._log = ctx.log
        self._debug = config.get("debug", False)
        self._discovery_timeout = int(config.get("discovery_timeout_ms", 2000))
        self._renewal_interval = float(config.get("renewal_interval_seconds", 300))
        self._protocol_port = int(config.get("protocol_port", 0))
        self._sidecar_port = int(config.get("sidecar_port", 0))

        self._upnp = None
        self._mappings = []
        self._external_ip = ""
        self._last_renew = time.monotonic()

        if miniupnpc is None:
            self._log.warning("UPNP_SKIP miniupnpc not installed")
            return

        self._upnp = miniupnpc.UPnP()
        self._upnp.discoverdelay = self._discovery_timeout

        # Discover and map on init
        self._discover_and_map()

    def _discover_and_map(self):
        """Discover UPnP gateway and request port mappings."""
        try:
            devices = self._upnp.discover()
            if devices == 0:
                self._log.info("UPNP_INIT no gateway found")
                return
            self._upnp.selectigd()
            self._external_ip = self._upnp.externalipaddress()
            self._log.info(f"UPNP_INIT gateway found, external_ip={self._external_ip}")

            if self._protocol_port > 0:
                self._add_mapping(self._protocol_port, "TCP", f"Knarr protocol {self._protocol_port}")
            if self._sidecar_port > 0:
                self._add_mapping(self._sidecar_port, "TCP", f"Knarr sidecar {self._sidecar_port}")

        except Exception as e:
            self._log.info(f"UPNP_INIT not available ({e})")

    def _add_mapping(self, port: int, protocol: str, description: str):
        """Request a single port mapping."""
        try:
            self._upnp.addportmapping(
                port, protocol, self._upnp.lanaddr, port, description, ""
            )
            self._mappings.append((port, protocol, description))
            self._log.info(f"UPNP_MAP {protocol} port={port}")
        except Exception as e:
            self._log.warning(f"UPNP_MAP_FAIL port={port} err={e}")

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        """Renew port mappings periodically."""
        if not self._upnp or not self._mappings:
            return

        now = time.monotonic()
        if now - self._last_renew < self._renewal_interval:
            return

        self._last_renew = now
        for port, protocol, description in self._mappings:
            try:
                self._upnp.addportmapping(
                    port, protocol, self._upnp.lanaddr, port, description, ""
                )
            except Exception:
                pass

        if self._debug:
            self._log.info(f"UPNP_RENEW mappings={len(self._mappings)}")

    async def on_shutdown(self) -> None:
        """Remove all port mappings."""
        if not self._upnp:
            return
        for port, protocol, _ in self._mappings:
            try:
                self._upnp.deleteportmapping(port, protocol)
                self._log.info(f"UPNP_UNMAP port={port}")
            except Exception:
                pass
        self._mappings.clear()

    @property
    def external_ip(self) -> str:
        return self._external_ip
