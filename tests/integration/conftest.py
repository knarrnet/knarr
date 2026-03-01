"""Disable sidecar auto-start in integration tests to avoid port collisions."""
import pytest
from knarr.dht.node import DHTNode

_original_init = DHTNode.__init__


def _init_no_sidecar(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    if self._config.get("node", {}).get("sidecar_port") is None:
        self._config.setdefault("node", {})["sidecar_port"] = 0


@pytest.fixture(autouse=True)
def _disable_sidecar(monkeypatch):
    monkeypatch.setattr(DHTNode, "__init__", _init_no_sidecar)
