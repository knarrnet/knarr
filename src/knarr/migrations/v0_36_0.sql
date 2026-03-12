-- v0.36.0: Settlement support (Handsal sprint)

-- Index for fast settlement history queries (B4 cockpit endpoints)
CREATE INDEX IF NOT EXISTS idx_receipt_log_doc_type
    ON receipt_log (document_type);

CREATE INDEX IF NOT EXISTS idx_receipt_log_counterparty
    ON receipt_log (counterparty);

-- Version marker (idempotent)
INSERT OR REPLACE INTO schema_version (version) VALUES ('0.36.0');
