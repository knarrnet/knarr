# BCW — Blockchain Watcher Plugin

On-chain payment detection and receipt generation for Solana. Poll + optional
WebSocket streaming.

## What it does

BCW watches Solana addresses for SOL and SPL token transfers, classifies them
(payment received, payment sent, internal transfer, withdrawal), and writes
signed receipts into a local SQLite store. Each transfer emits a bus event
that other plugins or the agent can subscribe to.

Peer addresses are derived deterministically from a master seed, so every peer
gets a unique deposit address without any coordination.

Since v0.49.0 the plugin supports an optional WebSocket subscription for
low-latency payment detection; the polling path still runs as a gap-recovery
backstop so no transfers are lost across reconnects.

## Prerequisites

- PyNaCl (already a knarr dependency)
- A 32-byte hex master seed stored in the vault
- `websockets>=12.0` — optional, installed by default; required only if you
  configure `ws_url`

## Setup

### 1. Store the master seed

Generate a 32-byte random seed and store it via the cockpit API:

```bash
# Generate a seed (do this once, back it up securely)
python -c "import secrets; print(secrets.token_hex(32))"

# Store it in the vault
curl -sk -X PUT \
  http://127.0.0.1:<cockpit_port>/api/secrets/bcw/bcw_master_seed \
  -H "Authorization: Bearer <cockpit_token>" \
  -H "Content-Type: application/json" \
  -d '{"value": "<your_64_char_hex_seed>"}'
```

The cockpit token is in `.node/.cockpit_token` (or wherever your data
directory lives).

Without a valid seed the plugin silently disables itself on startup.

### 2. Configure the chain

The plugin ships with a default `plugin.toml` that configures Solana
mainnet. Override in your `knarr.toml` if needed:

```toml
[plugins.blockchain-watcher]
enabled = true
poll_interval_seconds = 10
sol_min_balance = 0.01                     # emit wallet.sol_low below this

[[plugins.blockchain-watcher.chains]]
chain_id = "solana-mainnet"
rpc_url = "https://api.mainnet-beta.solana.com"
ws_url = "wss://api.mainnet-beta.solana.com"   # optional — enables streaming
tokens = ["KNARR"]
commitment = "finalized"
min_amount_lamports = 10000

[plugins.blockchain-watcher.chains.token_mints]
KNARR = "KNRRmint111111111111111111111111111111111111"
```

For **testnet/devnet**, change `chain_id`, `rpc_url`, and (if used) `ws_url`:

```toml
[[plugins.blockchain-watcher.chains]]
chain_id = "solana-devnet"
rpc_url = "https://api.devnet.solana.com"
ws_url = "wss://api.devnet.solana.com"
tokens = ["KNARR"]
commitment = "finalized"
min_amount_lamports = 10000
```

The plugin accepts any `chain_id` starting with `solana-`. Invalid chain
IDs fail loudly at startup (v0.45.0) — if `enabled = true` but no chain
resolves, the plugin disables itself and logs a warning naming the
offending `chain_id`. No more silent no-ops.

### 3. Verify

After restarting the node, check the logs for:

```
BCW disabled: missing/invalid bcw_master_seed in vault
```

If you see this, the seed wasn't stored correctly. Otherwise the plugin is
active and polling (and streaming, if `ws_url` is set).

## Watchlist

The plugin automatically watches:

- **Master address** — derived from the raw master seed
- **Own node** — derived from master seed + your node ID
- **All peers** — derived from master seed + each peer's node ID (updated every tick)

External addresses can be added via bus events:

```python
bus.emit("bcw.watch_request", node_id="<64-char-hex>", chain_id="solana-mainnet")
bus.emit("bcw.unwatch", node_id="<64-char-hex>", chain_id="solana-mainnet")
```

## Transfer classification

| from_self | to_self | to_known_wallet | Type |
|-----------|---------|-----------------|------|
| no | yes | - | `payment_received` |
| yes | yes | - | `wallet_transfer` |
| yes | no | yes | `payment_executed` |
| yes | no | no | `wallet_withdrawal` |

Each classified transfer carries a `correlation_id` (v0.49.0) threaded
through the receipt, dedup key, and emitted events so consumers can
reconcile WS-delivered and poll-recovered copies of the same transfer.

## Bus events

**Payment detection:**
- `payment.received.solana` — incoming payment detected
- `payment.received.solana-<chain>` — thin WS-delivered variant (gap-recovery emits the richer form)
- `payment.finalized.solana` — incoming payment reached finalized commitment
- `payment.sent.solana` — outgoing payment detected
- `payment.sent.finalized.solana` — outgoing payment finalized
- `wallet_transfer` — internal transfer between self-owned addresses
- `wallet_withdrawal` — outgoing transfer to unknown address

**Operational:**
- `bcw.address_assigned` — new watch address derived for a peer
- `bcw.capabilities` — published once after first successful RPC call
- `wallet.sol_low` — master wallet SOL balance below `sol_min_balance`
  (checked every 300s since v0.42.0)

## Data storage

The plugin stores its state in `bcw.sqlite3` inside the plugin directory:

- `bcw_watchlist` — address-to-node mapping with cursor tracking + `correlation_id`
- `bcw_seen` — deduplication table (chain_id + tx_hash + tx_index)
- `bcw_receipts` — signed receipt documents with dedup keys

## Known limitations

- Solana-only. Other chains require a new module following `solana.py`.
- Public RPC endpoints rate-limit aggressively; a dedicated RPC provider
  is strongly recommended for production loads.
- WebSocket reconnect intervals are not yet operator-tunable (CR tracked
  in backlog).
