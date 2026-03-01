-- v0.23.0: Peer encryption key, execution receipt, provider address on skills
-- All ALTER TABLE statements are idempotent (fail silently if column exists)

-- Peer encryption_key for opportunistic encryption
ALTER TABLE peers ADD COLUMN encryption_key TEXT DEFAULT '';

-- Execution receipt column
ALTER TABLE execution_log ADD COLUMN receipt TEXT DEFAULT '';

-- Provider address columns on skills (gossip-reachable providers)
ALTER TABLE skills ADD COLUMN provider_host TEXT DEFAULT '';
ALTER TABLE skills ADD COLUMN provider_port INTEGER DEFAULT 0;

-- Remote job tracking columns on async_jobs
ALTER TABLE async_jobs ADD COLUMN provider_node_id TEXT;
ALTER TABLE async_jobs ADD COLUMN provider_host TEXT;
ALTER TABLE async_jobs ADD COLUMN provider_port INTEGER;
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON async_jobs(input_hash);
