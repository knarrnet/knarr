# v0.46.0 Test Hygiene — Batch 3 Review

**Sprint:** v0.46.0 Test Hygiene
**Batch:** 3 (Section 5: TLS; Section 8: Punchhole frontend)
**Status:** PENDING GPT REVIEW

## Files Changed

### 1. `tests/unit/test_tls.py`

**Root cause:** `test_key_pem_permissions` checks `os.stat(key_path).st_mode & 0o777 == 0o600`. On Windows, `chmod 0600` is a no-op — NTFS has no Unix permission bits. The test failed on this machine (Windows 11).

**Fix:**
- Added `import sys`
- Added `@pytest.mark.skipif(sys.platform == "win32", reason="chmod 0600 not applicable on Windows")` to `test_key_pem_permissions`

**No behavioral change:** The permission check is correct on POSIX. The skip is Windows-only. All other 9 tests continue to pass.

---

### 2. `tests/unit/test_punchhole.py`

**Three separate root causes were identified and fixed:**

#### 2a. `asyncio.get_event_loop().run_until_complete()` ordering failures

**Root cause:** `TestStartupSequence::test_startup_emits_fill_before_ready` and `TestFrontendCacheHitMiss` (7 tests) all used `asyncio.get_event_loop().run_until_complete(...)`. In Python 3.12+, this fails in combined runs when a prior async test's event loop has been cleaned up.

**Fix:** Replaced all 7 occurrences of `asyncio.get_event_loop().run_until_complete(` with `asyncio.run(` (replace_all).

#### 2b. `PunchholeFrontendPlugin.__init__` calls `asyncio.ensure_future()` (requires running loop)

**Root cause:** `PunchholeFrontendPlugin.__init__` calls `asyncio.ensure_future(self._bus_loop())` at line 88 of the frontend handler. `asyncio.ensure_future()` requires a running event loop (it calls `get_running_loop()` internally). When the `TestFrontendCacheHitMiss` tests ran as sync `def` tests, there was no running loop → `RuntimeError`.

`asyncio.run(...)` (fix 2a above) would have fixed the `_startup()` call, but not the constructor — `asyncio.run()` is not running when the `plugin = PunchholeFrontendPlugin(...)` line executes.

**Fix:** Converted all 7 methods in `TestFrontendCacheHitMiss` from sync `def` to `@pytest.mark.asyncio` + `async def`. Replaced `asyncio.run(X)` with `await X`. Now pytest-asyncio provides a running loop for each test, so both the constructor's `ensure_future` and the subsequent `await` calls succeed.

#### 2c. `ctx.state_dir` not set in fixture (disclosure log DB path resolution)

**Root cause:** `PunchholeFrontendPlugin.__init__` resolves the disclosure log path as `ctx.state_dir or ctx.plugin_dir`. `make_mock_ctx()` set `ctx.plugin_dir = tmp_path` but did not set `ctx.state_dir`. Since `ctx` is a `MagicMock()`, `ctx.state_dir` auto-creates as a truthy `MagicMock`, so `state_dir = MagicMock()` instead of `tmp_path`. Then `str(MagicMock() / "test_disc7.db")` produces an invalid path → `sqlite3.OperationalError: unable to open database file`.

**Fix:** Added `ctx.state_dir = None` to `make_mock_ctx()`. With `None`, the `or` fallback uses `ctx.plugin_dir` (the actual `tmp_path`), and the disclosure DB opens correctly.

---

## Results

```
42 passed, 1 skipped in 0.71s
```

- `test_key_pem_permissions`: skipped on Windows (correct)
- All 42 remaining tests: pass
- "Task was destroyed but pending" warnings: benign (backend plugin background tasks that aren't cancelled on teardown — not failures)

---

## Nuances for GPT

1. **`asyncio.ensure_future()` vs `asyncio.run()`**: `asyncio.run()` creates a fresh event loop and blocks until the coroutine completes. `asyncio.ensure_future()` schedules a task on the *currently running* loop. When constructing `PunchholeFrontendPlugin` inside an `async def` test, `asyncio.ensure_future()` finds the running loop provided by pytest-asyncio. This is the correct fix.

2. **`ctx.state_dir = None`**: This is a legitimate fixture gap. The source was updated to use `state_dir` as a preferred override of `plugin_dir` for DB file placement (allowing plugins to store state in a separate directory). The mock never set this attribute, so MagicMock's auto-attribute created a truthy garbage value.

3. **"Task was destroyed" warnings**: `PunchholeBackendPlugin` and `PunchholeFrontendPlugin` spawn background asyncio tasks (`_startup`, `_stale_loop`, `_miss_loop`, `_bus_loop`) that run forever until explicitly cancelled. The tests don't call a teardown method, so when pytest-asyncio closes the event loop, the tasks are cancelled with a warning. This is a test hygiene gap (missing `await plugin.shutdown()` or similar) but is not a test failure. Section 6 (HUNG) addresses teardown issues — this is a less severe version of that.

4. **`_simulate_fill` return type**: `_simulate_fill` is a sync function that returns `_noop()` (a coroutine). `await _simulate_fill(...)` first calls the sync function (which mutates `plugin._cache`), then awaits the returned `_noop()` coroutine. The await is a no-op, but required to avoid "coroutine was never awaited" warnings.

## GPT Review

Verified against the current worktree state and a targeted run of the two files in this environment: `43 passed in 37.72s`. On this Linux/WSL run the TLS permission test was not skipped; the Windows-only skip condition is therefore unexercised here but still correct for the platform-specific failure described in the brief.

### 1. `tests/unit/test_tls.py`

- **CORRECT FIX** — the permission assertion is testing a real POSIX behavior: `generate_tls_cert()` calls `os.chmod(key_path, 0o600)` at [tls.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/mail/tls.py#L64), so keeping the assertion on POSIX is correct. Skipping `test_key_pem_permissions` only on `sys.platform == "win32"` is appropriate because the test inspects Unix mode bits via `os.stat(...).st_mode & 0o777`, which is not a meaningful portability contract on Windows. This is a targeted platform guard, not a weakened behavioral check.

### 2. `tests/unit/test_punchhole.py`

- **CORRECT FIX** — replacing the sync `run_until_complete(...)` usage with `asyncio.run(...)` in the backend startup test matches the Python 3.12-safe pattern for running a coroutine from sync test code. `PunchholeBackendPlugin._startup()` is an async coroutine at [handler.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/plugins/09-punchhole-backend/handler.py#L235), and `asyncio.run(plugin._startup())` in [test_punchhole.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/tests/unit/test_punchhole.py#L588) is the correct replacement for ordering-sensitive `get_event_loop().run_until_complete(...)`.
- **CORRECT FIX** — converting the seven `TestFrontendCacheHitMiss` methods to `@pytest.mark.asyncio` + `async def` is the right fix for the constructor-time task scheduling issue. `PunchholeFrontendPlugin.__init__` resolves `ctx.subscribe_events(...)` and then calls `asyncio.ensure_future(self._bus_loop())` at [handler.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/plugins/08-punchhole-frontend/handler.py#L82). Precise nuance: `ensure_future()` uses `asyncio.get_event_loop()` and then `loop.create_task()`, so the requirement is an available current loop; after async-test cleanup, sync construction can fail when pytest leaves no current loop. Running these tests under `pytest.mark.asyncio` provides the necessary loop context, and the subsequent direct `await` calls are therefore aligned with source behavior.
- **CORRECT FIX** — setting `ctx.state_dir = None` in `make_mock_ctx()` is a legitimate fixture repair. The frontend now prefers `ctx.state_dir or ctx.plugin_dir` when resolving the disclosure DB path at [handler.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/plugins/08-punchhole-frontend/handler.py#L77). Leaving `state_dir` unset on a `MagicMock` produces a truthy mock instead of `None`, so path concatenation targets garbage rather than the real temp directory. Forcing `None` restores the intended fallback to `plugin_dir`.
- **CORRECT FIX** — the `_simulate_fill` helper usage is also coherent with the implementation. The helper mutates `plugin._cache` synchronously and then returns a no-op coroutine in [test_punchhole.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/tests/unit/test_punchhole.py#L756), so `await _simulate_fill(...)` is the right way to avoid stray “coroutine was never awaited” noise while preserving the synchronous cache mutation.
- **CORRECT FIX** — the batch does not mask the remaining pending-task warnings. The targeted run still emitted `Task was destroyed but it is pending!` warnings because both punchhole plugins schedule long-lived background tasks in their constructors at [handler.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/plugins/08-punchhole-frontend/handler.py#L88) and [handler.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/plugins/09-punchhole-backend/handler.py#L219). That is a residual teardown gap, but it is not evidence that these test fixes were “changed to pass.”

BATCH 3 APPROVED

