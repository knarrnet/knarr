-- v0.36.0: Settlement support (Handsal sprint)

-- Settlement state tracking: one row per bilateral peer relationship.
-- Tracks cadence, amounts, and reconciliation state.
-- No new receipt tables needed — all settlement receipts go in existing
-- receipt_log (append-only, indexed by document_type).
CREATE TABLE IF NOT EXISTS settlement_state (
    peer_key                  TEXT PRIMARY KEY,
    last_settlement_at        REAL,
    last_settlement_amount    REAL,
    consecutive_settlements   INTEGER DEFAULT 0,
    last_reconciliation_at    REAL
);

-- Index for fast settlement history queries (B4 cockpit endpoints)
CREATE INDEX IF NOT EXISTS idx_receipt_log_doc_type
    ON receipt_log (document_type);

CREATE INDEX IF NOT EXISTS idx_receipt_log_counterparty
    ON receipt_log (counterparty);

-- Version marker (idempotent)
INSERT OR REPLACE INTO schema_version (version) VALUES ('0.36.0');
