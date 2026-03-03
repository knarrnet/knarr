"""
Receipt writer — bridges Document layer to storage layer.

Replaces the old _write_receipt() method body. The node imports this
and delegates. The writer knows nothing about document shapes (Layer 1
handles that) or signing details (Layer 2 handles that).

Usage in node.py:
    from ..commerce.receipt_writer import write_receipt

    def _write_receipt(self, doc, counterparty=None, order_ref=None, sign=False):
        return write_receipt(
            doc, self.node_info.node_id, self._signing_key,
            self.storage, counterparty, order_ref, sign, self.bus,
        )
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from nacl.signing import SigningKey

    from .documents import Document

logger = logging.getLogger(__name__)


def write_receipt(
    doc: "Document",
    identity: str,
    signing_key: Optional["SigningKey"],
    storage,
    counterparty: Optional[str] = None,
    order_ref: Optional[str] = None,
    sign: bool = False,
    bus=None,
) -> str:
    """Write a Document to the append-only receipt_log.

    Callers construct the Document (Layer 1), this function handles
    canonical serialization, optional signing (Layer 2), and storage.

    Returns the receipt_id.
    """
    receipt_id = doc["receipt_id"]
    payload = doc.payload  # returns a defensive copy

    if sign and signing_key:
        from ..core.proof import sign_document

        verification_method = f"did:knarr:{identity}#key-1"
        secured = sign_document(payload, signing_key, verification_method)
        payload_json = json.dumps(secured, sort_keys=True, separators=(",", ":"))
        signature = secured["proof"]["proofValue"]
    else:
        payload_json = doc.canonical_json()
        signature = None

    logger.debug(
        f"RECEIPT_WRITE type={doc.document_type} id={receipt_id} "
        f"order={str(order_ref)[:8] if order_ref else 'none'} signed={sign}"
    )

    try:
        storage.write_receipt(
            receipt_id=receipt_id,
            document_type=doc.document_type,
            timestamp=doc["timestamp"],
            identity=identity,
            counterparty=counterparty,
            order_ref=order_ref,
            proof_purpose="assertionMethod" if sign else "none",
            payload_json=payload_json,
            signature=signature,
        )
    except Exception as exc:
        logger.warning(
            f"RECEIPT_WRITE_FAIL type={doc.document_type} id={receipt_id}: {exc}"
        )
        if bus:
            bus.emit(
                "receipt.write_failed",
                document_type=doc.document_type,
                receipt_id=receipt_id,
                error=str(exc),
                identity=identity,
            )

    return receipt_id
