-- v0.29.1: Mail bucket storage — separate inbox/jobreport/system tables

CREATE TABLE IF NOT EXISTS mail_inbox (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    timestamp REAL NOT NULL,
    body TEXT NOT NULL,
    session_id TEXT,
    msg_type TEXT DEFAULT 'text',
    reply_to TEXT,
    ttl_expires REAL NOT NULL,
    status TEXT DEFAULT 'unread',
    created_at REAL NOT NULL DEFAULT 0,
    system INTEGER DEFAULT 0,
    item_origin TEXT DEFAULT 'skill'
);

CREATE TABLE IF NOT EXISTS mail_jobreport (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    timestamp REAL NOT NULL,
    body TEXT NOT NULL,
    session_id TEXT,
    msg_type TEXT DEFAULT 'text',
    reply_to TEXT,
    ttl_expires REAL NOT NULL,
    status TEXT DEFAULT 'unread',
    created_at REAL NOT NULL DEFAULT 0,
    system INTEGER DEFAULT 0,
    item_origin TEXT DEFAULT 'skill'
);

CREATE TABLE IF NOT EXISTS mail_system (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    timestamp REAL NOT NULL,
    body TEXT NOT NULL,
    session_id TEXT,
    msg_type TEXT DEFAULT 'text',
    reply_to TEXT,
    ttl_expires REAL NOT NULL,
    status TEXT DEFAULT 'unread',
    created_at REAL NOT NULL DEFAULT 0,
    system INTEGER DEFAULT 0,
    item_origin TEXT DEFAULT 'skill'
);

-- Indexes per bucket
CREATE INDEX IF NOT EXISTS idx_mail_inbox_status ON mail_inbox(status);
CREATE INDEX IF NOT EXISTS idx_mail_inbox_expires ON mail_inbox(ttl_expires);
CREATE INDEX IF NOT EXISTS idx_mail_inbox_session ON mail_inbox(session_id);
CREATE INDEX IF NOT EXISTS idx_mail_inbox_from ON mail_inbox(from_node);
CREATE INDEX IF NOT EXISTS idx_mail_inbox_type ON mail_inbox(msg_type);

CREATE INDEX IF NOT EXISTS idx_mail_jobreport_status ON mail_jobreport(status);
CREATE INDEX IF NOT EXISTS idx_mail_jobreport_expires ON mail_jobreport(ttl_expires);

CREATE INDEX IF NOT EXISTS idx_mail_system_status ON mail_system(status);
CREATE INDEX IF NOT EXISTS idx_mail_system_expires ON mail_system(ttl_expires);

-- Redistribute existing data from mail table into buckets
INSERT OR IGNORE INTO mail_jobreport SELECT * FROM mail WHERE msg_type LIKE 'knarr/system/task_result%';
INSERT OR IGNORE INTO mail_system SELECT * FROM mail WHERE system = 1 AND msg_type NOT LIKE 'knarr/system/task_result%';
INSERT OR IGNORE INTO mail_inbox SELECT * FROM mail WHERE system = 0 AND msg_type NOT LIKE 'knarr/system/task_result%';

-- Preserve original table as backup (rename, don't drop)
ALTER TABLE mail RENAME TO mail_legacy;
