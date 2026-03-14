# Removal Manifest — v0.46.0 Test Hygiene

Tests removed during this sprint. Each entry requires a rationale.
No silent deletions.

| File | Functions removed | Rationale |
|------|-------------------|-----------|
| `tests/unit/test_upnp.py` | All (5 tests) | `knarr.dht.upnp` module was removed in an earlier sprint; UPnP functionality moved to the `02-upnp` plugin. Module no longer exists in source, tests cannot be fixed without re-implementing the deleted module. |
