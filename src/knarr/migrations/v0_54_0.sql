-- v0.54.0: URI envelope receipt indexing
ALTER TABLE receipt_log ADD COLUMN uri TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_receipt_log_uri ON receipt_log(uri);
UPDATE receipt_log
SET uri = 'knarr://' || identity || '/c/receipt/' || receipt_id
WHERE uri = '';
