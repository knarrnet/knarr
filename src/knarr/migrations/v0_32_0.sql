-- v0.32.0: mail_creditnote bucket for signed credit notes

CREATE TABLE IF NOT EXISTS mail_creditnote (
    rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT UNIQUE NOT NULL,
    from_node    TEXT NOT NULL,
    to_node      TEXT NOT NULL,
    timestamp    REAL NOT NULL,
    body         TEXT NOT NULL,
    session_id   TEXT,
    msg_type     TEXT DEFAULT 'knarr/commerce/credit_note',
    reply_to     TEXT,
    ttl_expires  REAL NOT NULL,
    status       TEXT DEFAULT 'unread',
    created_at   REAL NOT NULL DEFAULT 0,
    system       INTEGER DEFAULT 0,
    item_origin  TEXT DEFAULT 'receipt'
);

CREATE INDEX IF NOT EXISTS idx_mail_creditnote_status ON mail_creditnote(status);
CREATE INDEX IF NOT EXISTS idx_mail_creditnote_expires ON mail_creditnote(ttl_expires);
CREATE INDEX IF NOT EXISTS idx_mail_creditnote_from ON mail_creditnote(from_node);
CREATE INDEX IF NOT EXISTS idx_mail_creditnote_ref ON mail_creditnote(session_id);
