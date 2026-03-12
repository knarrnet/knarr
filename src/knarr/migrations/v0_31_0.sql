-- v0.31.0: Economy foundation — add consumer-side ledger columns
ALTER TABLE ledger ADD COLUMN prepaid REAL DEFAULT 0.0;
ALTER TABLE ledger ADD COLUMN pub_tab REAL DEFAULT 0.0;
ALTER TABLE ledger ADD COLUMN soft_limit REAL DEFAULT -5.0;
ALTER TABLE ledger ADD COLUMN hard_limit REAL DEFAULT -10.0;
-- v0.31.0: Add provider_public_key to async_jobs for receipt verification (Sonnet V-13)
ALTER TABLE async_jobs ADD COLUMN provider_public_key TEXT;
