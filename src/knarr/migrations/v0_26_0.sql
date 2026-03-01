-- v0.26.0: Mail correspondents, pull columns, jurisdiction, trust

-- Mail correspondents table for Tier 2 pull
CREATE TABLE IF NOT EXISTS mail_correspondents (
    node_id TEXT PRIMARY KEY,
    last_sent REAL,
    last_received REAL
);

-- Outbox columns for pull delivery tracking
ALTER TABLE mail_outbox ADD COLUMN push_failed_at REAL;
ALTER TABLE mail_outbox ADD COLUMN pull_delivered_at REAL;

-- Jurisdiction field on peers (DHT-computed groups)
ALTER TABLE peers ADD COLUMN jurisdiction TEXT DEFAULT '';

-- Trust column on ledger (group-seeded initial trust)
ALTER TABLE ledger ADD COLUMN trust REAL DEFAULT 0.3;
