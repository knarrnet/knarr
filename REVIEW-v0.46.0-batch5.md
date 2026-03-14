# v0.46.0 Test Hygiene — Batch 5 Review

**Sprint:** v0.46.0 Test Hygiene
**Batch:** 5 (Section 3: Security/Economy tests)
**Status:** PENDING GPT REVIEW

## Files Changed

### 1. `tests/unit/test_adversary_regression_v36.py`

**Two root causes fixed:**

#### 1a. `_make_settle_body` missing required schema fields

**Root cause:** `validate_settle_request` (schemas.py:67) requires `current_balance`, `credit_limit`, `provider_wallet` (32–44 char string), and `timestamp`. `_make_settle_body` already supplied `timestamp` (0.0) but omitted the other three. The handler returned early at validation for all tests that used `_make_settle_body`.

**Fix:** Added the three fields to the returned dict:
```python
"current_balance": -amount,
"credit_limit": 3.0,
"provider_wallet": "A" * 32,
```

#### 1b. `TestH5_SettleRequestReplay::test_duplicate_receipt_id_rejected` — wrong assertion target

**Root cause:** `handle_settle_request` (handlers.py:134–140) calls `node.storage.queue_settlement` via `_enqueue_write`, not `node._plugins.on_inbound_settlement`. The plugin hook was removed in the commerce handler refactor (v0.35.0+). The test asserted `on_inbound_settlement.assert_called_once()` after the first delivery, which always fails because the handler never calls it.

**Fix:** Changed assertions from `on_inbound_settlement` to `queue_settlement`:
```python
# First delivery
node.storage.queue_settlement.assert_called_once()
# Reset
node.storage.queue_settlement.reset_mock()
# Second delivery (dedup)
node.storage.queue_settlement.assert_not_called()
```

**Security invariant preserved:** The dedup check (`if existing is not None: return` at handlers.py:131) still blocks the second call — `queue_settlement` is not called on replay. The test correctly verifies this.

**Result:** All 18 tests in `test_adversary_regression_v36.py` pass.

---

### 2. `tests/unit/test_v0_33_0.py`

**Three root causes fixed:**

#### 2a. `TestCommerceRefundCap::test_refund_capped_at_2x_original` — stale 2x cap assumption

**Root cause:** The old handler had a 2x cumulative refund cap using `get_cumulative_refund`. The current handler (handlers.py:100–103) uses a simpler **1x per-note cap**: `max_refund = original["price"]` and `if amount > max_refund: return`. The test sent `amount=10.0` against `original["price"]=10.0`, so `10.0 > 10.0` is False → handler did NOT reject → `_enqueue_write` WAS called → `assert_not_called()` failed.

**Fix:**
- Renamed test to `test_refund_capped_at_1x_original`
- Changed `amount=10.0` → `amount=11.0` (exceeds 1x cap of 10.0)
- Removed `node.storage.get_cumulative_refund.return_value = 15.0` (method no longer called)
- Updated docstring and inline comment

**Result:** Handler rejects `11.0 > 10.0`, `_enqueue_write` not called, assertion passes.

#### 2b. `TestMinimumPriceFloor` — `MagicMock(spec=DHTNode)` intercepts `_resolve_price_builtin`

**Root cause:** `DHTNode._resolve_price(node, ...)` calls `self._resolve_price_builtin(...)` internally. When `node = MagicMock(spec=DHTNode)`, `node._resolve_price_builtin` is a `MagicMock` that returns another `MagicMock` when called. Python's `MagicMock.__iter__` returns an empty iterator by default, so `price, breakdown = MagicMock()` raises `ValueError: not enough values to unpack (expected 2, got 0)`.

**Fix:** Added `import types` and bound the real implementation to the mock node in both tests:
```python
node._resolve_price_builtin = types.MethodType(DHTNode._resolve_price_builtin, node)
```
This ensures `_resolve_price` delegates to the real `_resolve_price_builtin` with the mock node as `self`. The mock node already exposes the correct `_config`, `_group_engine`, and `storage` attributes, so the real method executes correctly.

**Verification:**
- `test_global_minimum_price_applied`: base_price=0.1, minimum_price=0.5 → price=0.5 → `price >= 0.5` ✓
- `test_no_minimum_price_default`: base_price=0.0 → early return (0.0, breakdown) → `price >= 0.0` ✓

#### 2c. `tests/unit/test_economy.py` — two additional A1.2 fixtures (discovered during batch run)

**Root cause:** Same A1.2 security rule as Batches 3–4 — `get_or_create_ledger_entry` always stores `balance=0.0` in the DB regardless of `initial_balance`.

- `test_ledger_entry_creation_with_initial_credit`: Called with `initial_balance=3.0`, asserted `entry.balance == 3.0`. DB stores 0.0 → fails.
- `test_variable_pricing`: Created entry with `initial_balance=3.0`, applied `update_ledger_provider(key, 2.0)`, asserted `balance == 1.0` (3.0 − 2.0). DB starts at 0.0 → 0.0 − 2.0 = −2.0 → fails.

**Fix:**
- `test_ledger_entry_creation_with_initial_credit`: Changed `initial_balance=3.0` → `0.0`, assertion `3.0` → `0.0`. Documents A1.2 behavior.
- `test_variable_pricing`: Changed `initial_balance=3.0` → `0.0`, added direct SQL UPDATE to set balance=3.0 (same pattern as `test_credit_balancer.py`). Assertion `1.0` preserved — confirms provider charge deducts correctly.

**Result:** All 18 tests in `test_economy.py` pass.

---

## Combined Section 3 Batch Results

```
164 passed, 1 warning in 0.92s
```

Files tested:
- `tests/unit/test_adversary_regression_v36.py`
- `tests/unit/test_v0_33_0.py`
- `tests/unit/test_v0_32_0_exploits.py`
- `tests/unit/test_bus_events.py`
- `tests/unit/test_economy.py`
- `tests/unit/test_e2e_paths.py`
- `tests/unit/test_receipt_writes_v34.py`

No failures. No source changes.

---

## Nuances for GPT

1. **No source changes in this batch.** All 6 fixes are fixture/test repairs. The security rules (A1.2, 1x refund cap, schema validation requirements) are correct. Only the tests' premises about historical behavior were wrong.

2. **`_make_settle_body` fix is load-bearing for the whole adversary regression suite.** With the missing schema fields, ALL tests using `_make_settle_body` were silently passing `assert_not_called()` for the wrong reason (schema validation rejection, not the actual attack surface). After the fix, those tests now exercise the actual rejection logic they were written for. The one exception was `TestH5_SettleRequestReplay` which used `assert_called_once()` — that's the only one that FAILED.

3. **`on_inbound_settlement` vs `queue_settlement`**: The `handle_settle_request` handler in the current codebase never calls `on_inbound_settlement`. It queues via `storage.queue_settlement`. The H5 test's assertion was testing a removed code path. The dedup invariant (second delivery rejected) is still correctly verified — just against `queue_settlement` instead.

4. **`test_economy.py` A1.2 scope**: The sprint spec section 3 listed 7 files. `test_economy.py` was already in scope (it's one of the 7 files). The 2 failures there were not listed in the pre-batch spec because they hadn't been hit yet — they were discovered when running the full section batch. They are the same A1.2 class as all prior economy fixture fixes.

5. **`types.MethodType` bind**: This is the minimal invasive fix for the MagicMock spec interception problem. The alternative (`patch.object`) would work too, but `types.MethodType` is cleaner for unbound-method delegation and doesn't affect test isolation. The mock node's `_config` and `storage` attributes are already set correctly for the real `_resolve_price_builtin` to execute.

## GPT Review

The described fixes match the current codebase and the observed failures. `validate_settle_request()` now requires `current_balance`, `credit_limit`, `provider_wallet`, and `timestamp` at [schemas.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/commerce/schemas.py#L63), so extending `_make_settle_body()` in `test_adversary_regression_v36.py` is necessary to exercise the real settle-request path. The replay assertion update is also correct: `handle_settle_request()` dedups on `accepted_receipt_id` and then enqueues `storage.queue_settlement` via `_enqueue_write`; it does not call `on_inbound_settlement` in the current handler flow at [handlers.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/commerce/handlers.py#L115).

The `test_v0_33_0.py` adjustments are also correct. The credit-note handler now enforces a simple per-note `1x` cap using `original["price"]` at [handlers.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/commerce/handlers.py#L96), so changing the test to reject `11.0` against an original `10.0` matches source behavior. I also reproduced the `MagicMock(spec=DHTNode)` issue directly: `price, breakdown = DHTNode._resolve_price(...)` raises `ValueError: not enough values to unpack` unless the real `_resolve_price_builtin` is rebound onto the mock with `types.MethodType`, so that fix is well-founded.

The `test_economy.py` changes are consistent with the A1.2 rule in [storage.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/dht/storage.py#L1121): new ledger rows are inserted with `balance=0.0`, while `update_ledger_provider()` subtracts from the stored DB balance at [storage.py](/mnt/f/knarr.clean/.worktrees/v0.46.0-test-hygiene/src/knarr/dht/storage.py#L1175). Using direct SQL to seed a non-zero working balance in `test_variable_pricing` is the same legitimate fixture pattern already used in prior balance tests.

One nuance: the batch still leaves a deprecation warning in `test_adversary_regression_v36.py` because `_run()` uses `asyncio.get_event_loop().run_until_complete(...)` without creating a loop first on Python 3.12. That warning is already reflected in the batch result and is not a reason to reject this test-hygiene change set.

## Validation

- `python3 -m pytest tests/unit/test_adversary_regression_v36.py -q`
  - `18 passed, 1 warning in 0.90s`
- `python3 -m pytest tests/unit/test_adversary_regression_v36.py tests/unit/test_v0_33_0.py tests/unit/test_v0_32_0_exploits.py tests/unit/test_bus_events.py tests/unit/test_economy.py tests/unit/test_e2e_paths.py tests/unit/test_receipt_writes_v34.py -q`
  - `164 passed, 1 warning in 8.91s`

**Verdict:** `BATCH 5 APPROVED`
