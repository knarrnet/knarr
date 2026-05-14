"""Protocol constants for the Knarr network.

A-08: cluster-aware $KNR mint resolution. Default cluster: devnet (interim — KNR
mainnet mint pending). Set KNARR_CLUSTER=mainnet to override once KNR mainnet
address is filled.

Operators select the cluster via the ``KNARR_CLUSTER`` environment variable;
the selected cluster pins ``KNARR_MINT`` at import time. Unknown cluster values
fail loud.

Why env rather than a config file key:
  * Mint identity is a protocol constant, not an operator knob. A node that
    talks to mainnet mints with a devnet ACL is a security event, not a
    runtime typo — the fast-fail boundary must be process-level.
  * The Token-2022 contract address is immutable per cluster. No use case
    exists for swapping it at runtime within a single process.
"""
import os

# Per-cluster mint addresses. Token-2022 on the respective Solana cluster.
# Mainnet-beta: pending KNR mint. Empty until new address is set.
# Devnet: no canonical KNR mint. ``chain.py`` applies the same ``""``
# convention for devnet/testnet token_mint so callers fail closed.
KNARR_MINT_MAINNET = ""
KNARR_MINT_DEVNET = ""

_CLUSTER_MINTS = {
    "mainnet": KNARR_MINT_MAINNET,
    "devnet": KNARR_MINT_DEVNET,
}

KNARR_CLUSTER = os.environ.get("KNARR_CLUSTER", "devnet").strip().lower() or "devnet"

if KNARR_CLUSTER not in _CLUSTER_MINTS:
    raise ValueError(
        f"KNARR_CLUSTER={KNARR_CLUSTER!r} is not a recognised cluster. "
        f"Valid values: {sorted(_CLUSTER_MINTS)}."
    )

KNARR_MINT = _CLUSTER_MINTS[KNARR_CLUSTER]
KNARR_DECIMALS = 9
KNARR_SYMBOL = "KNR"
