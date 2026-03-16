"""Global test fixtures — resource leak prevention."""
import sys
import os as _os

# Force local src onto sys.path ahead of any installed knarr package.
# Prevents installed package from shadowing local src imports.
_src = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import asyncio
import concurrent.futures
import pytest
from knarr.dht.node import DHTNode

# OOM guard: full test suite crashes Windows (110GB RAM exhausted).
# Run in batches of ~20 files. Override with: pytest --allow-full-suite
MAX_COLLECTED = 600


def pytest_addoption(parser):
    parser.addoption("--allow-full-suite", action="store_true", default=False,
                     help="Bypass the OOM guard that limits collected tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--allow-full-suite"):
        return
    if len(items) > MAX_COLLECTED:
        pytest.exit(
            f"\n  OOM GUARD: {len(items)} tests collected (limit {MAX_COLLECTED}).\n"
            f"  Full suite will crash this machine. Run specific files instead:\n"
            f"    pytest tests/unit/test_foo.py tests/unit/test_bar.py\n"
            f"  Or override with: pytest --allow-full-suite\n",
            returncode=3,
        )

_original_init = DHTNode.__init__
_active_nodes = []


def _init_lean(self, *args, **kwargs):
    """Wrap DHTNode.__init__ to use a small thread pool and disable sidecar in tests."""
    # Ensure event loop exists for DHTNode.__init__ (Python 3.12+ raises
    # RuntimeError from get_event_loop() when called outside async context)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    _original_init(self, *args, **kwargs)
    # Replace the 32-thread pool with a 2-thread pool
    self._handler_pool.shutdown(wait=False)
    self._handler_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    # Disable sidecar auto-start (avoids TLS cert FileNotFoundError in CI)
    if self._config.get("node", {}).get("sidecar_port") is None:
        self._config.setdefault("node", {})["sidecar_port"] = 0
    # V015: Clear loaded plugins to prevent filesystem scanning during tests
    if hasattr(self, '_plugins') and hasattr(self._plugins, 'plugins'):
        self._plugins.plugins.clear()
    _active_nodes.append(self)


@pytest.fixture(autouse=True)
def _lean_node(monkeypatch):
    """Reduce DHTNode thread pool size and clean up after each test."""
    monkeypatch.setattr(DHTNode, "__init__", _init_lean)
    yield
    # Cleanup all nodes created during this test
    for node in _active_nodes:
        try:
            node._handler_pool.shutdown(wait=False)
            node.storage.close()
        except Exception:
            pass
    _active_nodes.clear()
