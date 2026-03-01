"""Core egress filter — prevents exfiltration of key material.

This filter is HARDCODED. No config flag. No disable option.
A compromised plugin, agent, or skill cannot bypass it because it runs
outside the plugin boundary.
"""
import logging

from .wallet import b58encode

logger = logging.getLogger(__name__)

# Path patterns that should never appear in outbound data
_SENSITIVE_PATHS = [
    "node.key", "hot.key", "reserve.key", "drain.address",
    "keyring/", ".knarr/keyring", ".node/keyring",
]


class EgressFilter:
    """Scans outbound payloads for key material. Blocks exfiltration attempts."""

    def __init__(self):
        self._sensitive_bytes: list[bytes] = []

    def register_sensitive_material(self, material):
        """Register raw key bytes as sensitive. Called by core and plugins."""
        if isinstance(material, str):
            try:
                material = bytes.fromhex(material)
            except Exception:
                material = material.encode('utf-8')
        self._sensitive_bytes.append(material)

    def check(self, payload: str) -> bool:
        """Check a text payload for key material. Returns True if SAFE."""
        payload_lower = payload.lower()

        # 1. Raw key byte detection (hex-encoded form in text, case-insensitive)
        for material in self._sensitive_bytes:
            hex_form = material.hex()  # already lowercase
            if hex_form in payload_lower:
                logger.critical("EGRESS_BLOCK reason=raw_key_hex")
                return False

        # 2. Path pattern detection
        for path in _SENSITIVE_PATHS:
            if path.lower() in payload_lower:
                logger.critical(f"EGRESS_BLOCK reason=path_pattern match={path}")
                return False

        # 3. Base58 private key pattern (64-byte Ed25519 = 85-88 base58 chars)
        # Only flag if it matches a registered material's base58 encoding
        # (Too many false positives if we flag ALL long base58 strings)
        for material in self._sensitive_bytes:
            b58_form = b58encode(material)
            if b58_form in payload:
                logger.critical("EGRESS_BLOCK reason=b58_private_key")
                return False

        return True

    def check_binary(self, data: bytes) -> bool:
        """Lightweight check for binary payloads (sidecar). Path patterns only."""
        # Binary data (images, audio) gets path check only — no byte scanning.
        # Rationale: 32 random bytes appearing in a 10MB JPEG is astronomically
        # unlikely, and scanning is O(n). Path strings in binary data are meaningful.
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return True
        for path in _SENSITIVE_PATHS:
            if path.lower() in text.lower():
                logger.critical(f"EGRESS_BLOCK_BIN reason=path_pattern match={path}")
                return False
        return True
