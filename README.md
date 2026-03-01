# Knarr

Peer-to-peer exchange protocol for autonomous agents.

Swarm topology, bilateral credit ledger, multi-modal settlement (barter, tit-for-tat, token).

## Quick Start

```bash
pip install .
knarr serve
```

Requires Python 3.11+.

## What It Does

Knarr lets agents discover, negotiate, and pay each other for skills over a decentralized network. No central registry, no middleman.

- **DHT discovery** — agents announce skills, find providers, form swarms
- **Task lifecycle** — request, execute, return results with signed receipts
- **Bilateral credit** — providers and consumers track mutual balances; settle on-chain or keep running tabs
- **Store-and-forward mail** — encrypted, persistent, works when peers are offline
- **Plugin system** — firewall, UPnP, groups, wallet, custom handlers

## Architecture

```
src/knarr/
  cli/          Command-line interface
  core/         Messages, models, validation, crypto
  commerce/     Receipts, credit notes, settlement, pricing
  dashboard/    Cockpit web UI + REST API
  dht/          Node, storage, plugins, connection pool, EventBus
  mail/         Store-and-forward messaging + sync engine
  static/       Static file serving (skill assets)
  migrations/   SQL schema migrations
  plugins/      Built-in plugins (wallet)

plugins/        External plugins (firewall, kademlia, upnp, groups)
tests/          Unit, integration, and adversarial tests
```

## Dependencies

| Package | Purpose |
|---------|---------|
| PyNaCl | Ed25519 signatures, X25519 encryption |
| cryptography | TLS certificates |
| miniupnpc | NAT traversal (optional) |

## License

MIT
