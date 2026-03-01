-- v0.25.0: Commerce tables (settlement queue, tab reminders, pricing columns)

CREATE TABLE IF NOT EXISTS settlement_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    from_node TEXT NOT NULL,
    body TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    processed_at REAL
);

CREATE TABLE IF NOT EXISTS tab_reminders (
    peer_public_key TEXT PRIMARY KEY,
    last_sent REAL NOT NULL
);

-- Price column on execution_log
ALTER TABLE execution_log ADD COLUMN price REAL;
ALTER TABLE execution_log ADD COLUMN provider_node_id TEXT;
