-- v0.37.0: Vordur — Warehouse Manager quarantine table
--
-- DMZ quarantine: holds inbound documents that fail WM gates or are
-- configured for hold_for_review. Survives power loss. CRUD via storage.py.

CREATE TABLE IF NOT EXISTS dmz_quarantine (
    id                TEXT PRIMARY KEY,
    document_type     TEXT NOT NULL,
    document_json     TEXT NOT NULL,
    originator_pubkey TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    gate_results      TEXT,
    reason            TEXT,
    received_at       REAL NOT NULL,
    promoted_at       REAL,
    resolved_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_dmz_status ON dmz_quarantine(status);

-- Version marker (idempotent)
INSERT OR REPLACE INTO schema_version (version) VALUES ('0.37.0');
