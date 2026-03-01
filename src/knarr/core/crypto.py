import json
import base64
import hashlib
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
import logging

logger = logging.getLogger(__name__)

def verify_receipt(receipt_json: str, provider_public_key_hex: str) -> bool:
    """Decodes and verifies an execution receipt using the provider's Ed25519 public key.

    Args:
        receipt_json: JSON string with {"data": {payload...}, "signature": "<base64>"}
        provider_public_key_hex: Ed25519 public (verify) key as hex string.

    The signature covers the canonical JSON of the data dict.
    provider_node_id in the data must equal SHA-256(provider_public_key).
    """
    try:
        receipt = json.loads(receipt_json)
        if not isinstance(receipt, dict):
            return False

        data = receipt.get("data")
        signature_b64 = receipt.get("signature")
        if not isinstance(data, dict) or not signature_b64:
            return False

        # Verify provider identity: node_id == SHA-256(public_key)
        expected_node_id = hashlib.sha256(bytes.fromhex(provider_public_key_hex)).hexdigest()
        if data.get("provider_node_id") != expected_node_id:
            return False

        # Reconstruct canonical payload and verify signature
        payload_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')

        signature_bytes = base64.b64decode(signature_b64)
        verify_key = VerifyKey(bytes.fromhex(provider_public_key_hex))
        verify_key.verify(payload_bytes, signature_bytes)
        return True
    except (json.JSONDecodeError, BadSignatureError, ValueError, TypeError) as e:
        logger.debug(f"Receipt verification failed: {e}")
        return False
