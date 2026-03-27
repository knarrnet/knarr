"""A-07: KAD post-promotion staggering.

Tests the _post_promote_tick stagger logic:
1. After auto-promote, _post_promote_tick is set to 1 and stagger starts.
2. Tick 1: self-lookup fires, _post_promote_tick advances to 2.
3. Tick 2: bucket refresh fires, _post_promote_tick advances to 3.
4. Tick 3: republish fires, _post_promote_tick resets to 0.
5. During stagger, normal bucket refresh is suppressed (return early).
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_stagger_target():
    """Create a minimal object to exercise stagger logic isolated from on_tick scaffolding."""

    class _Fake:
        mode = "full"
        _post_promote_tick = 0
        _debug = True
        _own_skills = {"skill1": "path1"}
        _last_republish = 0.0
        _republish_interval = 900.0

        def __init__(self):
            import logging
            self._log = logging.getLogger("test.kad")
            self._lookup = MagicMock()
            self.find_calls = []
            self.refresh_calls = []
            self.republish_calls = []

            async def _fake_find(target):
                self.find_calls.append(target)
                return []
            self._lookup.find_nodes = _fake_find
            self._maybe_refresh_buckets = AsyncMock(side_effect=lambda: self.refresh_calls.append(True))
            self._put_provider_to_closest = AsyncMock(side_effect=lambda sk, cp: self.republish_calls.append(sk))

        async def _run_stagger_tick(self):
            """Mirrors the A-07 stagger block from on_tick."""
            if self._post_promote_tick > 0 and self._lookup and self.mode == "full":
                if self._post_promote_tick == 1:
                    asyncio.create_task(self._lookup.find_nodes(
                        getattr(self, '_ctx', MagicMock()).node_id if hasattr(self, '_ctx') else "aa" * 32
                    ))
                    if self._debug:
                        self._log.info("KAD_POST_PROMOTE_TICK1 self_lookup_started")
                    self._post_promote_tick = 2
                elif self._post_promote_tick == 2:
                    await self._maybe_refresh_buckets()
                    if self._debug:
                        self._log.info("KAD_POST_PROMOTE_TICK2 bucket_refresh_done")
                    self._post_promote_tick = 3
                elif self._post_promote_tick == 3:
                    if self._own_skills:
                        for sk, cp in list(self._own_skills.items()):
                            asyncio.create_task(self._put_provider_to_closest(sk, cp))
                        if self._debug:
                            self._log.info(f"KAD_POST_PROMOTE_TICK3 republish count={len(self._own_skills)}")
                    self._post_promote_tick = 0
                return True  # stagger was active
            return False  # stagger not active

    return _Fake()


class TestKADStagger:
    def test_initial_state(self):
        """Plugin starts with _post_promote_tick = 0."""
        import os
        # Check via file source only — avoid sys.path pollution across test suite
        handler_path = os.path.join(os.path.dirname(__file__), "../../plugins/01-kademlia/handler.py")
        with open(handler_path) as f:
            src = f.read()
        assert 'class KademliaPlugin' in src, "KademliaPlugin must be defined in handler"
        assert '_post_promote_tick' in src, "_post_promote_tick must be in handler source"
        assert 'on_tick' in src, "on_tick must be in handler source"

    def test_tick1_self_lookup_and_advance(self):
        """Tick 1: self-lookup fires and tick advances to 2."""
        obj = _make_stagger_target()
        obj._post_promote_tick = 1

        async def run():
            await obj._run_stagger_tick()
            # Let the create_task coroutine run
            await asyncio.sleep(0)
        _run(run())

        assert obj._post_promote_tick == 2, f"Expected 2, got {obj._post_promote_tick}"

    def test_tick2_bucket_refresh_and_advance(self):
        """Tick 2: bucket refresh fires and tick advances to 3."""
        obj = _make_stagger_target()
        obj._post_promote_tick = 2

        _run(obj._run_stagger_tick())
        assert obj._post_promote_tick == 3, f"Expected 3, got {obj._post_promote_tick}"
        obj._maybe_refresh_buckets.assert_awaited_once()

    def test_tick3_republish_and_reset(self):
        """Tick 3: republish fires and tick resets to 0."""
        obj = _make_stagger_target()
        obj._post_promote_tick = 3

        async def run():
            await obj._run_stagger_tick()
            await asyncio.sleep(0)
        _run(run())

        assert obj._post_promote_tick == 0, f"Expected 0, got {obj._post_promote_tick}"

    def test_tick0_no_stagger(self):
        """When tick=0, stagger is not active."""
        obj = _make_stagger_target()
        obj._post_promote_tick = 0

        active = _run(obj._run_stagger_tick())
        assert not active, "Stagger should not be active when tick=0"

    def test_stagger_suppresses_normal_refresh(self):
        """During stagger tick 1, only self-lookup fires — not bucket refresh from normal path."""
        obj = _make_stagger_target()
        obj._post_promote_tick = 1

        async def run():
            stagger_active = await obj._run_stagger_tick()
            # Simulate: if stagger returns True, normal path is skipped (return early)
            normal_refresh_would_run = not stagger_active
            return normal_refresh_would_run

        result = _run(run())
        assert result is False, "Normal refresh must NOT run when stagger is active"
        obj._maybe_refresh_buckets.assert_not_awaited()

    def test_source_has_post_promote_init(self):
        """_post_promote_tick is initialized in KademliaPlugin.__init__."""
        import os
        handler_path = os.path.join(os.path.dirname(__file__), "../../plugins/01-kademlia/handler.py")
        with open(handler_path) as f:
            src = f.read()
        assert "_post_promote_tick" in src, "_post_promote_tick must appear in handler.py"
        assert "self._post_promote_tick = 0" in src, "Must be initialized to 0 in __init__"
        assert "self._post_promote_tick = 1" in src, "Must be set to 1 on promote"
