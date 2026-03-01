-- v0.31.0: Economy foundation — add consumer-side ledger columns
ALTER TABLE ledger ADD COLUMN prepaid REAL DEFAULT 0.0;
ALTER TABLE ledger ADD COLUMN pub_tab REAL DEFAULT 0.0;
ALTER TABLE ledger ADD COLUMN soft_limit REAL DEFAULT 0.0;
ALTER TABLE ledger ADD COLUMN hard_limit REAL DEFAULT 0.0;
