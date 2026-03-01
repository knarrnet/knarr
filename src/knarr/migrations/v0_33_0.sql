-- v0.33.0: cumulative refund tracking (B3/S-021)
ALTER TABLE execution_log ADD COLUMN refund_total REAL NOT NULL DEFAULT 0.0;
