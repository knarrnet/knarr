# v0.51.0 — "KAD Complete"

**Date:** 2026-03-23
**Branch:** `feature/v0.51.0` (pending cluster gate → merge to main)
**Baseline:** v0.50.0

---

## Summary

Kademlia DHT hardening sprint. 17 items across 2 tracks. The KAD plugin moves from a passive cache to a production-grade DHT with persistence, security, and correctness guarantees. Bounty/escrow pricing is now supported.

## Track A — Bounty/Escrow Enablement

### ESC-01: Skill-sheet decoupled from validation
Skills with invalid fields (e.g., missing metadata, bad format) remain directly callable by whitelisted peers. Previously, a validation failure in `announce()` left the skill handler registered but returned "Skill sheet missing" on every call. Now the raw skill sheet is populated before validation runs — failed validation prevents DHT announcement but not local use.

### ESC-02: Negative-price skill announcements
The `price < 0` guard in validation has been replaced with a `math.isfinite()` check. Negative prices represent bounties — the caller receives credit instead of being charged. The admission gate, pricing engine, and config system already support negative prices; the validation guard was the last blocker. NaN and Inf remain rejected. The `price > 1000.0` cap is unchanged.

## Track B — KAD Complete (15 items)

### Persistence (KAD-04)
Routing table and provider cache now persist to SQLite (`kademlia.db` in data_dir). Routing table checkpoints every 5 minutes and flushes on shutdown. Provider cache uses write-through persistence. Monotonic timestamps are correctly converted to wall-clock for cross-restart survival. Expired records are filtered on load.

### Security Hardening
- **KAD-05:** PUT_PROVIDER proximity validation — rejects stores from senders farther than 2× the K-th closest node (bootstrap phase exempt)
- **KAD-06:** Ed25519 provider record signatures via new `sign_bytes` callback on PluginContext. Signature identity binding ensures pubkey corresponds to sender node_id
- **KAD-11:** BEP-5 announce tokens — GET_PROVIDERS must precede PUT_PROVIDER. HMAC tokens with 5-minute windows and clock-skew tolerance
- **KAD-12:** IP diversity cap — max 2 nodes per IP per bucket, enforced on add and DB reload
- **KAD-13:** Disjoint lookup paths — configurable D parallel lookups with independent seed groups
- **KAD-15:** Input validation on `get_closest` — rejects malformed hex IDs gracefully

### Routing & Liveness
- **KAD-01:** Bootstrap self-lookup wrapper — `iterative_find_node()` now exists on the plugin, enabling active routing table population after join
- **KAD-03:** Auto-promote passive → full after 60s uptime + 5 peers. `passive_locked` mode available for monitoring-only nodes
- **KAD-09:** Ping-before-evict on full k-buckets — oldest peer gets a PING; evicted only if no PONG within timeout
- **KAD-10:** PING/PONG RPC for dedicated liveness probes

### Performance & Correctness
- **KAD-02:** Republish cycle — own provider records refreshed every 900s (half TTL) with GET-before-STORE token acquisition
- **KAD-07:** Key canonicalization — `strip().lower().rstrip("/")` before hashing. Case, trailing slashes, and whitespace no longer fragment discovery
- **KAD-08:** Min-heap eviction in ProviderCache — O(log N) replaces O(N) linear scan, with periodic heap compaction
- **KAD-14:** Query fan-out rate limiting — max 5 iterative lookups per minute (configurable), prevents unbounded network fan-out on cache misses

## Adversary Fixes (11 applied)

| TP | Severity | Fix |
|----|----------|-----|
| TP-1 | HIGH | Unified key space — all paths use `default_key_function` |
| TP-2 | HIGH | Signature identity binding (SHA-256 pubkey = sender node_id) |
| TP-3 | HIGH | `evict_expired()` now deletes from SQLite |
| TP-4 | HIGH | Gossip fallback when all token fetches fail (prevents black hole) |
| TP-5 | HIGH | Signature verification based on payload presence, not receiver capability |
| TP-6 | MEDIUM | Unsigned PUT skipped on signing failure |
| TP-8 | MEDIUM | Heap compaction when stale entries exceed 3× live records |
| TP-9 | MEDIUM | `remove_peer()` writes through to SQLite |
| TP-10 | MEDIUM | Pending eviction cleanup on peer removal |
| TP-12 | MEDIUM | DB reload uses `add_peer()` for IP diversity enforcement |
| TP-15 | LOW | Self-lookup triggered after auto-promotion |

## Panel

- **Dev panel:** 4 seats (Gemini 3.1 Pro, GPT-5.4, Sonnet 4.6, Opus 4.6)
- **Winners:** Sonnet 12 items, GPT 5 items (protocol interop)
- **Judge panel:** GPT-5.4, Gemini 3.1 Pro, Opus 4.6 + Forseti tiebreaker
- **Adversary panel:** GPT-5.4, Gemini 3.1 Pro, Sonnet 4.6, Opus 4.6

## Testing

- 243 test files, ~1990 tests pass
- 17 new v51 test files (83 tests)
- 20 regression tests pass
- 39/39 Wave 1 contract tests pass
- 15/15 adversary exploit tests pass (inverted post-fix)
- 0 unresolved sprint-introduced failures

## Files Changed

| File | Change |
|------|--------|
| `src/knarr/dht/node.py` | ESC-01: pre-validation skill population |
| `src/knarr/core/validation.py` | ESC-02: negative price + isfinite guard |
| `src/knarr/dht/plugins.py` | KAD-06: sign_bytes field on PluginContext |
| `src/knarr/commerce/plugin_bridge.py` | KAD-06: _SignBytesCallback |
| `plugins/00-kademlia/handler.py` | KAD-01,02,03,05,06,07,09,10,11,14 + 7 TPs |
| `plugins/00-kademlia/kbuckets.py` | KAD-04,09,12,15 + 3 TPs |
| `plugins/00-kademlia/providers.py` | KAD-04,08 + 2 TPs |
| `plugins/00-kademlia/lookup.py` | KAD-13 + TP-1 |
| `plugins/00-kademlia/plugin.toml` | KAD-03 passive_locked + config keys |

**30 files changed, +3042 insertions, -81 deletions.**
