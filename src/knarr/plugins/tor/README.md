# Tor Transport Plugin

Onion routing for Knarr peers. Adds outbound SOCKS5 to `.onion` addresses and
optionally publishes the node itself as a Tor v3 hidden service.

**Safety posture:** the plugin is triple-gated and inert by default. It loads
with zero effect unless you explicitly (1) install the extra, (2) run a Tor
daemon it can reach, and (3) set `enabled = true` in your node config.

---

## What it does

1. **Outbound `.onion` routing** — when a peer advertises a `.onion` host,
   Knarr's connection pool dials it through your local Tor SOCKS5 port.
   Clearnet peers are unaffected.
2. **Hidden service publishing** (optional) — derives a v3 onion address
   from a dedicated Ed25519 key and lets you advertise it as your node's
   `advertise_host`. Other peers with the plugin reach you over Tor.
3. **Daemon health** — connects to the Tor control port, watches
   `NEWCONSENSUS` / `STATUS_GENERAL` / `CIRC` events, and emits bus events
   for consensus loss, circuit failures, clock skew, and `DANGEROUS_SOCKS`
   warnings.
4. **Circuit budget** — caps circuit-attempt rates per peer and globally
   so one noisy peer cannot exhaust Tor resources.

The plugin does **not** force all traffic through Tor. Clearnet peers stay
on clearnet; `.onion` peers go through Tor. Operators who want everything
over Tor should firewall off direct outbound networking separately.

---

## Prerequisites

- A running Tor daemon with:
  - SOCKS5 port open locally (default `127.0.0.1:9050`)
  - Control port open with cookie or password auth (default `127.0.0.1:9051`)
- The `tor` Python extra: `pip install knarr[tor]` (installs
  `python-socks[asyncio]`)

Without the extra, the plugin logs `tor.dependency_missing` on init and
remains a silent no-op — so an operator can install `knarr` without ever
touching Tor.

---

## Install the Tor daemon

**Debian / Ubuntu:**

```
sudo apt install tor
```

Edit `/etc/tor/torrc`, add:

```
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
CookieAuthFileGroupReadable 1
```

Add your user to the `debian-tor` group so Knarr can read the cookie:

```
sudo usermod -aG debian-tor $USER
sudo systemctl restart tor
```

**Windows (Tor Browser bundle or `tor.exe`):** the cookie path defaults to
`%APPDATA%\tor\control_auth_cookie`. If you use password auth instead, set
`control_auth_method = "password"` and `control_password = "..."` in the
plugin config.

**macOS (Homebrew):**

```
brew install tor
```

Edit `/usr/local/etc/tor/torrc` (same contents as Debian example).

---

## Enable the plugin

Three gates must all flip:

### 1. Install the extra

```
pip install knarr[tor]
```

### 2. Reach a Tor daemon

Verify the SOCKS and control ports respond:

```
curl --socks5 127.0.0.1:9050 https://check.torproject.org
echo -e 'PROTOCOLINFO 1\r\nQUIT\r\n' | nc 127.0.0.1 9051
```

The control port reply must start with `250-PROTOCOLINFO 1` and include a
`Tor/...` version line. Anything else and the plugin refuses to talk to
the port (an operator left an unrelated service on 9051 once — the plugin
fails closed).

### 3. Opt in via config

In your `knarr.toml`:

```toml
[plugins.tor]
enabled = true
```

Restart the node. On startup you should see `TOR_ENABLE_OK` in the log
and `tor.ready` on the bus.

---

## Config reference

All keys shown with defaults. Override any subset in `[plugins.tor]`.

```toml
[plugins.tor]
enabled = false                      # master gate
socks_host = "127.0.0.1"             # where Tor's SOCKS5 listens
socks_port = 9050
control_port = 9051
control_auth_method = "cookie"       # "cookie" | "password" | "none"
control_cookie_file = ""             # blank → auto-detect per-OS default
control_password = ""                # required when auth_method = "password"

# Identity mode. "separate" (default) keeps hidden-service identity distinct
# from the node's Ed25519 key — safe. "shared" linkably reuses the node key
# as the hidden-service key, which is what most "single-identity" deployments
# want but REQUIRES the interlock below to acknowledge the linkage.
key_mode = "separate"
acknowledge_identity_leak = false

# Hidden-service publication
advertise_onion = true               # export derived .onion as advertise_host
prefer_onion = false                 # prefer .onion over clearnet for peers
                                     # that advertise both
derived_onion_fallback = true        # if own HS hasn't published yet, fall
                                     # back to a derived onion during bootstrap
derived_onion_fallback_window = 300  # seconds before the fallback sticks

# Circuit budget
circuit_sharing = "per_peer"         # or "per_pubkey" to share a bucket across
                                     # peer_ids with the same pubkey
max_circuits_per_peer_per_minute = 10
max_circuits_global_per_minute = 60
collapse_pubkey_aliases = true       # share budget across aliased peer_ids
slow_circuit_warning_ms = 5000       # emit tor.circuit_slow above this

# Sidecar exposure — opt-in only
expose_sidecar = false               # allow sidecar-over-Tor
sidecar_allow_unauth = false         # never true in production
```

### `key_mode` — read this before flipping to `shared`

The default `separate` mode mints a dedicated Ed25519 key for the hidden
service. Your onion address is not linkable to your node identity over the
wire.

`shared` mode reuses the node's identity key as the HS key. Peers can
trivially confirm that node `X` and onion `Y` are the same entity. This is
often desirable (it reduces key sprawl and lets peers verify your HS
belongs to you), but the trade-off is explicit: set
`acknowledge_identity_leak = true` or the plugin refuses to start in
`shared` mode.

See `docs/specs/SPEC-tor-plugin.md` §5.1 for the full threat model.

---

## Bus events emitted

| Event | Meaning |
|-------|---------|
| `tor.ready` | Plugin fully initialized, daemon verified |
| `tor.dependency_missing` | `python-socks` not installed — plugin is a no-op |
| `tor.daemon_unreachable` | Control port disconnected or never connected |
| `tor.consensus_lost` | Tor lost directory consensus — outbound broken |
| `tor.consensus_recovered` | Consensus restored |
| `tor.clock_skew_warning` | Tor detected host clock skew (payload: `skew_seconds`) |
| `tor.circuit_failed` | Circuit teardown (payload: `circ_id`, `reason`) |
| `tor.circuit_slow` | Circuit built but slower than `slow_circuit_warning_ms` |
| `tor.advertise_host_overridden` | Node published onion as `advertise_host` |

Subscribe via `ctx.bus.subscribe("tor.*", handler)` from any other plugin.

---

## Troubleshooting

**`tor.dependency_missing` at startup** — you did not install the extra.
Run `pip install knarr[tor]` in the same venv the node uses.

**`control port non-Tor reply`** — something other than Tor is listening
on `control_port`. Check `ss -tlnp | grep 9051` or Windows equivalent.

**`AUTHENTICATE failed: cookie file unreadable`** — the node process
cannot read Tor's auth cookie. On Debian, add your user to `debian-tor`
and re-login. Alternatively switch to password auth.

**Onion peers unreachable but clearnet works** — check the bus for
`tor.consensus_lost`. If persistent, Tor has no consensus and cannot build
circuits; restart the daemon.

**Circuit budget denials** — look for `CIRCUIT_BUDGET_DENY` in logs with
`reason=per_peer|global|pubkey_collapse`. Raise `max_circuits_*` if the
denial pattern matches legitimate traffic.

---

## Further reading

- Spec: `docs/specs/SPEC-tor-plugin.md` (v1.1)
- Synthesis notes: `docs/specs/SYNTHESIS-tor-plugin.md`
- Tor control-port reference: https://spec.torproject.org/control-spec/
