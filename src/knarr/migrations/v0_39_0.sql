-- v0.39.0: Midgard economy + cockpit wiring

ALTER TABLE ledger ADD COLUMN held_balance REAL DEFAULT 0.0;

CREATE TABLE IF NOT EXISTS meter (
    actor TEXT NOT NULL,
    skill TEXT NOT NULL,
    qualifier TEXT DEFAULT '',
    count INTEGER DEFAULT 0,
    first_at REAL,
    last_at REAL,
    window_seconds REAL DEFAULT 0,
    PRIMARY KEY (actor, skill, qualifier)
);

DROP TABLE IF EXISTS settlement_state;

CREATE TABLE IF NOT EXISTS payment_receipts (
    tx_digest TEXT PRIMARY KEY,
    amount INTEGER NOT NULL,
    asset TEXT NOT NULL,
    destination TEXT NOT NULL,
    verified_at REAL NOT NULL
);
