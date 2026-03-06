-- v0.38.0 Ledger Semantic Migration
-- A1.1: Backup existing ledger before value transform
-- Python migration applies: UPDATE ledger SET balance = balance - economy.default_soft_limit

-- Step 1: create backup snapshot
CREATE TABLE IF NOT EXISTS bilateral_positions_v037 AS SELECT * FROM ledger;

-- Step 2: balance transform is applied by Python migration code in storage.py
-- (requires reading config for economy.default_soft_limit, so cannot be pure SQL)

-- Step 3: ensure new columns exist (added in v0.35.0 migration, idempotent here)
-- prepaid, pub_tab, soft_limit, hard_limit, credit_limit already present from v0.35.0.
-- No schema changes needed in v0.38.0.
