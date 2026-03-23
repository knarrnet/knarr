"""
PluginContext bridge — exposes sign_document and query_receipts to plugins.

Unblocks Thrall v3.3 (identity signing for settlement) and v0.36.0 E2E flow.

Integration in node.py plugin loader (~5 LOC):
    from ..commerce.plugin_bridge import make_sign_callback, make_query_receipts_callback

    ctx = PluginContext(
        # ... existing wiring ...
        sign_document=make_sign_callback(self._signing_key, self.node_info.node_id),
        query_receipts=make_query_receipts_callback(self.storage),
    )

PluginContext additions (plugins.py, 2 lines):
    sign_document: Optional[Callable] = None      # v0.35.0: sign dict per eddsa-jcs-2022
    query_receipts: Optional[Callable] = None      # v0.35.0: query receipt_log with filters
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

_MAX_QUERY_LIMIT = 1000


class _SignCallback:
    """Callable class for sign_document — prevents closure introspection."""

    __slots__ = ("_key", "_method")

    def __init__(self, signing_key: "SigningKey", node_id: str) -> None:
        self._key = signing_key
        self._method = f"did:knarr:{node_id}#key-1"

    def __call__(self, document: dict, proof_purpose: str = "assertionMethod") -> dict:
        from ..core.proof import sign_document

        return sign_document(document, self._key, self._method, proof_purpose)


class _QueryCallback:
    """Callable class for query_receipts — prevents closure introspection."""

    __slots__ = ("_storage",)

    def __init__(self, storage) -> None:
        self._storage = storage

    def __call__(
        self,
        document_type: Optional[str] = None,
        counterparty: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        return query_receipts(
            self._storage, document_type=document_type,
            counterparty=counterparty, since=since, limit=limit,
        )


class _SignBytesCallback:
    """KAD-06: Callable for raw-bytes signing for KAD provider record signatures.

    Distinct from sign_document (which uses JCS-2022 proof envelopes) —
    KAD records sign raw byte payloads for compactness.
    """

    __slots__ = ("_key",)

    def __init__(self, signing_key: "SigningKey") -> None:
        self._key = signing_key

    def __call__(self, data: bytes) -> "tuple[bytes, str]":
        sig = self._key.sign(data).signature
        pubkey_hex = self._key.verify_key.encode().hex()
        return (sig, pubkey_hex)


def make_sign_bytes_callback(signing_key: "SigningKey") -> Callable:
    """KAD-06: Create a sign_bytes callback for PluginContext.

    Plugins call: sig_bytes, pubkey_hex = ctx.sign_bytes(data: bytes)
    """
    return _SignBytesCallback(signing_key)


def make_sign_callback(
    signing_key: "SigningKey",
    node_id: str,
) -> Callable:
    """Create a sign_document callback for PluginContext.

    Plugins call: ctx.sign_document(payload_dict) -> secured_dict
    Uses __slots__ callable class to prevent closure introspection.
    """
    return _SignCallback(signing_key, node_id)


def make_query_receipts_callback(storage) -> Callable:
    """Create a query_receipts callback for PluginContext.

    Plugins call: ctx.query_receipts(document_type=..., counterparty=..., since=..., limit=50)
    Uses __slots__ callable class to prevent closure introspection.
    """
    return _QueryCallback(storage)


def query_receipts(
    storage,
    document_type: Optional[str] = None,
    counterparty: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Query receipt_log with optional filters.

    Returns list of dicts with receipt fields + parsed payload.
    Designed to be both a storage helper and the backing implementation
    for the PluginContext callback.
    """
    # Clamp limit to [1, _MAX_QUERY_LIMIT]
    limit = max(1, min(limit, _MAX_QUERY_LIMIT))

    conn = storage._get_conn()
    clauses = []
    params: list = []

    if document_type:
        clauses.append("document_type = ?")
        params.append(document_type)
    if counterparty:
        clauses.append("counterparty = ?")
        params.append(counterparty)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)

    where = " AND ".join(clauses) if clauses else "1=1"
    sql = (
        f"SELECT receipt_id, document_type, timestamp, identity, counterparty, "
        f"order_ref, proof_purpose, payload_json, signature, created_at "
        f"FROM receipt_log WHERE {where} ORDER BY created_at DESC LIMIT ?"
    )
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = []
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row[7]) if row[7] else {}
        except (json.JSONDecodeError, TypeError):
            pass

        results.append({
            "receipt_id": row[0],
            "document_type": row[1],
            "timestamp": row[2],
            "identity": row[3],
            "counterparty": row[4],
            "order_ref": row[5],
            "proof_purpose": row[6],
            "payload": payload,
            "signature": row[8],
            "created_at": row[9],
        })

    return results
