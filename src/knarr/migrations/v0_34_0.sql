-- v0.34.0: Receipt Foundation
CREATE TABLE IF NOT EXISTS receipt_log (
    receipt_id TEXT PRIMARY KEY,
    document_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    identity TEXT NOT NULL,
    counterparty TEXT,
    order_ref TEXT,
    proof_purpose TEXT NOT NULL DEFAULT 'assertion',
    payload_json TEXT NOT NULL,
    signature TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipt_log_type_ts ON receipt_log(document_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_receipt_log_order ON receipt_log(order_ref) WHERE order_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_receipt_log_identity ON receipt_log(identity, timestamp);
