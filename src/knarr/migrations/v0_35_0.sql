-- v0.35.0: Commerce Foundation
--
-- prepaid, pub_tab, soft_limit, hard_limit already added in v0.31.0.
-- This migration adds credit_limit and fixes the v0.31.0 defaults
-- (soft/hard were 0.0, should be -5.0/-10.0).

ALTER TABLE ledger ADD COLUMN credit_limit REAL NOT NULL DEFAULT 3.0;

-- Fix v0.31.0 defaults: ONLY update rows that still have both limits at the
-- v0.31.0 bug defaults (0.0/0.0). If an operator changed either value, both
-- are left alone — this protects intentional zero-tolerance configurations.
UPDATE ledger SET soft_limit = -5.0, hard_limit = -10.0
    WHERE soft_limit = 0.0 AND hard_limit = 0.0;

-- Discount rules table (pricing engine source)
CREATE TABLE IF NOT EXISTS discount_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    skill_group TEXT NOT NULL DEFAULT '*',
    effect_pct REAL NOT NULL DEFAULT 0.0,
    priority INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discount_rules_group ON discount_rules(group_name, active);

-- Admission decision log (append-only audit trail)
CREATE TABLE IF NOT EXISTS admission_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_key TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    effective_price REAL NOT NULL,
    balance_before REAL,
    balance_after REAL,
    reason TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admission_log_caller ON admission_log(caller_key, timestamp);
CREATE INDEX IF NOT EXISTS idx_admission_log_outcome ON admission_log(outcome, timestamp);
