-- v0.56.0: TOFU TLS certificate pinning
ALTER TABLE peers ADD COLUMN tls_cert_fingerprint TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_peers_tls_cert_fingerprint ON peers(tls_cert_fingerprint);

-- v0.56.0: URI column expansion across transport and commerce tables
ALTER TABLE skills ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE skills SET uri = 'knarr://' || provider_node_id || '/s/' || lower(skill_key) WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_skills_uri ON skills(uri);

ALTER TABLE tasks ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE tasks SET uri = 'knarr://' || provider_node_id || '/s/' || lower(skill_name) WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_tasks_uri ON tasks(uri);

ALTER TABLE execution_log ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE execution_log SET uri = 'knarr://' || provider_node_id || '/s/' || lower(skill_name) WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_execution_log_uri ON execution_log(uri);

ALTER TABLE async_jobs ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE async_jobs SET uri = 'knarr://' || provider_node_id || '/s/' || lower(skill_name) WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_async_jobs_uri ON async_jobs(uri);

ALTER TABLE mail_inbox ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE mail_inbox SET uri = 'knarr://' || to_node || '/m/' || message_id WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_mail_inbox_uri ON mail_inbox(uri);

ALTER TABLE mail_outbox ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE mail_outbox
SET uri = 'knarr://' || COALESCE(json_extract(body_json, '$.from_node'), '') || '/m/' || item_id
WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_mail_outbox_uri ON mail_outbox(uri);

ALTER TABLE settlement_queue ADD COLUMN uri TEXT NOT NULL DEFAULT '';
UPDATE settlement_queue SET uri = 'knarr://' || from_node || '/c/' || id WHERE uri = '';
CREATE INDEX IF NOT EXISTS idx_settlement_queue_uri ON settlement_queue(uri);

-- Adversary #11 fix (v0.56.0): has_pending_settlement dedup collision.
-- Previously, has_pending_settlement used `body LIKE '%KEY[:32]%'` — a
-- substring match on the first 32 chars of the peer public key against the
-- JSON body text. Two attack vectors:
--   1. Vanity-key collision: adversary generates a peer pubkey sharing the
--      first 32 hex chars with a victim (feasible with targeted GPU work).
--   2. Substring false positives: a body field that incidentally contains
--      the key as a substring could match unrelated entries.
-- False dedups → silent settlement drops → financial reconciliation failures.
-- Fix: add a dedicated peer_public_key column, populate for existing rows,
-- and rewrite has_pending_settlement to use exact match on the column.
--
-- Post-review fix (v0.56.0, Opus subagent): the column must store the actual
-- public key (matching has_pending_settlement() callers in netting.py:38 and
-- node.py:6013), not a node_id. The pre-review backfill coalesced to
-- counterparty_node_id / peer_node_id / from_node — all node_id-semantic —
-- which guaranteed dedup misses on every netting cycle. The corrected
-- backfill chain: try body pubkey fields first (peer_public_key, peer_key,
-- counterparty_key — all populated by node.py:_build_settlement_confirmation_body
-- and netting.py:queue_settlement bodies), then reverse-lookup from_node via
-- peer_keys table, then as a last resort fall through to node_id-semantic
-- fields so the dedup column still has *something* unique per row (at the
-- cost of dedup missing across heterogeneous representations for legacy
-- rows — acceptable for pre-existing rows, new rows are correct).
ALTER TABLE settlement_queue ADD COLUMN peer_public_key TEXT NOT NULL DEFAULT '';
UPDATE settlement_queue
SET peer_public_key = COALESCE(
    json_extract(body, '$.peer_public_key'),
    json_extract(body, '$.peer_key'),
    json_extract(body, '$.counterparty_key'),
    (SELECT public_key FROM peer_keys WHERE peer_keys.node_id = settlement_queue.from_node LIMIT 1),
    json_extract(body, '$.counterparty_node_id'),
    json_extract(body, '$.peer_node_id'),
    from_node
)
WHERE peer_public_key = '';
CREATE INDEX IF NOT EXISTS idx_settlement_queue_peer_pending
    ON settlement_queue(peer_public_key, status);
