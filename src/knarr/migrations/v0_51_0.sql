-- v0.51.0: Performance indexes + schema consolidation
-- Addresses BR-db-performance-audit-v051

-- async_jobs: status/expires queries are hot paths for cleanup
CREATE INDEX IF NOT EXISTS idx_async_jobs_status ON async_jobs(status);
CREATE INDEX IF NOT EXISTS idx_async_jobs_expires ON async_jobs(expires_at);
CREATE INDEX IF NOT EXISTS idx_async_jobs_updated ON async_jobs(updated_at);

-- skills: provider_node_id used in LEFT JOIN on every query_all_active_skills
CREATE INDEX IF NOT EXISTS idx_skills_provider ON skills(provider_node_id);

-- skills: announced_at + ttl used in expiry checks
CREATE INDEX IF NOT EXISTS idx_skills_announced ON skills(announced_at);

-- settlement_queue: status + priority used in get_settlement_items
CREATE INDEX IF NOT EXISTS idx_settlement_status ON settlement_queue(status, priority);

-- execution_log: created_at used in purge_execution_log_by_age
CREATE INDEX IF NOT EXISTS idx_execlog_created ON execution_log(created_at);

-- execution_log: consolidate runtime ALTER TABLE columns
ALTER TABLE execution_log ADD COLUMN quality_rating INTEGER;
ALTER TABLE execution_log ADD COLUMN refund_total REAL NOT NULL DEFAULT 0.0;

-- receipt_log: created_at for age-based pruning
CREATE INDEX IF NOT EXISTS idx_receipt_log_created ON receipt_log(created_at);
