"""Tor transport plugin for Knarr — SPEC-tor-plugin.md v1.1.

Synthesized from the 2026-04-11 blind parallel dev panel (Opus + Sonnet + GPT-5.4).
See F:\\thing\\specs\\SYNTHESIS-tor-plugin.md for the per-component picks.
"""
from .handler import TorPlugin, _CircuitBudget, onion_address_from_pubkey
from .control import AsyncControlPort, ControlPortError

__all__ = [
    "TorPlugin",
    "_CircuitBudget",
    "onion_address_from_pubkey",
    "AsyncControlPort",
    "ControlPortError",
]
