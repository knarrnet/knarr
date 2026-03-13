CREATE TABLE IF NOT EXISTS settlement_cadence (
    peer_key TEXT PRIMARY KEY,
    last_settlement_ts REAL NOT NULL
);
