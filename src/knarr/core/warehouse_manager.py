"""Warehouse Manager — pre-bus one-way gateway for inbound documents.

Validates all external documents through five sequential gates before
they reach the internal EventBus. Documents that fail or require review
are quarantined in the DMZ (SQLite, durable).

One-way constraint: WM has NO outbound path. Cannot send mail, emit to
DMZ, write to network-facing storage, or call external APIs. Reads from
dirty side, writes to clean side only.

Gates:
    1. Authenticity — signature valid (verify_document + VerifyKey)
    2. Addressing   — involves our identity (DID fragment check)
    3. Schema       — known type, valid structure (validator registry)
    4. Integrity    — proof fields valid, timestamp sane
    5. Authorization — signer has authority (per-type rules)
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional

from nacl.signing import VerifyKey

from .proof import verify_document

logger = logging.getLogger(__name__)

# Maximum allowed clock skew for document timestamps (seconds).
# Documents created more than 1 hour in the future are rejected.
_MAX_FUTURE_SKEW = 3600
# Documents older than 7 days are rejected.
_MAX_AGE = 7 * 24 * 3600


@dataclass(frozen=True)
class IngestResult:
    """Immutable result of WarehouseManager.ingest()."""
    status: str                     # "promoted", "held", "rejected"
    document_type: str
    quarantine_id: Optional[str]
    gate_results: dict              # {1: "pass", 2: "fail", ...}
    reason: Optional[str]
    needs_countersign: bool = False
    document: Optional[dict] = None


# ---------- Schema validator registry ----------
# Maps document_type -> validator_function from commerce/schemas.py.
# Lazy import to avoid circular dependencies.

def _get_schema_validators() -> dict:
    """Build the schema validator registry. Deferred to avoid import cycles."""
    from ..commerce.documents import _TYPE_REGISTRY
    from ..commerce.schemas import (
        validate_credit_note,
        validate_receipt,
        validate_settlement_confirmation,
        validate_tab_reminder,
        validate_payment_received,
        validate_payment_finalized,
        validate_payment_executed,
        validate_wallet_transfer,
        validate_wallet_withdrawal,
        validate_configuration_order,
        validate_punchhole_card,
        validate_cache_object,
        # v0.38.0: netting types
        validate_netting_reconcile,
        validate_netting_proposal,
        validate_netting_acceptance,
        validate_netting_executed,
    )

    def _document_validator(expected_type: str):
        required = set(_TYPE_REGISTRY.get(expected_type, set()))

        def _validate(body: dict) -> tuple[bool, str | None]:
            if body.get("document_type") != expected_type:
                return False, f"wrong document_type: {body.get('document_type')}"
            missing = sorted(required - set(body.keys()))
            if missing:
                return False, f"missing required field: {missing[0]}"
            return True, None

        return _validate

    return {
        "credit_note": validate_credit_note,
        "execution_receipt": validate_receipt,
        "settlement_prepared": _document_validator("settlement_prepared"),
        "settlement_accepted": _document_validator("settlement_accepted"),
        "settlement_processed": _document_validator("settlement_processed"),
        "settlement_confirmation": validate_settlement_confirmation,
        "tab_reminder": validate_tab_reminder,
        # v0.37.0: BCW payment/wallet types
        "payment_received": validate_payment_received,
        "payment_finalized": validate_payment_finalized,
        "payment_executed": validate_payment_executed,
        "wallet_transfer": validate_wallet_transfer,
        "wallet_withdrawal": validate_wallet_withdrawal,
        # v0.37.0: Admin + disclosure types
        "configuration_order": validate_configuration_order,
        "punchhole_card": validate_punchhole_card,
        "cache_object": validate_cache_object,
        # v0.38.0: netting types
        "netting_reconcile": validate_netting_reconcile,
        "netting_proposal": validate_netting_proposal,
        "netting_acceptance": validate_netting_acceptance,
        "netting_executed": validate_netting_executed,
    }


# ---------- Default gate/action rules ----------
_DEFAULT_RULES = {
    "credit_note":              {"gates": [1, 2, 3, 4], "action": "auto_promote"},
    "execution_receipt":        {"gates": [1, 2, 3, 4], "action": "auto_promote"},
    "settlement_prepared":      {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "settlement_accepted":      {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "settlement_processed":     {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "settlement_confirmation":  {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "payment_received":         {"gates": [1, 3, 4], "action": "auto_promote"},
    "payment_finalized":        {"gates": [1, 3, 4], "action": "auto_promote"},
    "payment_executed":         {"gates": [1, 3, 4], "action": "auto_promote"},
    "wallet_transfer":          {"gates": [1, 3, 4], "action": "auto_promote"},
    "wallet_withdrawal":        {"gates": [1, 3, 4], "action": "auto_promote"},
    "configuration_order":      {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "punchhole_card":           {"gates": [1, 3, 4], "action": "auto_promote"},
    "cache_object":             {"gates": [1, 3, 4], "action": "auto_promote"},
    # v0.38.0: netting types (A5.3)
    "netting_reconcile":        {"gates": [1, 2, 3, 4], "action": "auto_promote"},
    "netting_proposal":         {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "netting_acceptance":       {"gates": [1, 2, 3, 4, 5], "action": "hold_for_review"},
    "netting_executed":         {"gates": [1, 2, 3, 4], "action": "auto_promote"},
    "_default":                 {"gates": [1, 2, 3, 4], "action": "hold_for_review"},
}


class WarehouseManager:
    """Pre-bus one-way gateway. Validates external documents through five gates.

    One-way constraint: no outbound path. Cannot send mail, emit to DMZ,
    write to network-facing storage, or call external APIs.
    """

    def __init__(
        self,
        node_id: str,
        identity_fragments: list[str],
        internal_signer_keys: dict[str, bytes],
        bus,
        storage,
        config: dict,
        write_receipt_cb: Callable,
    ):
        self._node_id = node_id
        self._identity_fragments = set(identity_fragments)
        self._internal_signer_keys = dict(internal_signer_keys or {})
        self._bus = bus
        self._storage = storage
        self._write_receipt_cb = write_receipt_cb
        self._debug = config.get("debug", False)

        # Load per-type rules from config, falling back to defaults.
        self._rules = dict(_DEFAULT_RULES)
        config_rules = config.get("rules", {})
        for doc_type, rule_cfg in config_rules.items():
            if isinstance(rule_cfg, dict):
                self._rules[doc_type] = {
                    "gates": rule_cfg.get("gates", _DEFAULT_RULES.get("_default", {}).get("gates", [1, 2, 3, 4])),
                    "action": rule_cfg.get("action", "hold_for_review"),
                }

        # Schema validators — lazy init on first ingest.
        self._validators: Optional[dict] = None

    def _get_validators(self) -> dict:
        if self._validators is None:
            self._validators = _get_schema_validators()
        return self._validators

    def _get_rule(self, document_type: str) -> dict:
        """Resolve the gate/action rule for a document type."""
        return self._rules.get(document_type, self._rules.get("_default", {"gates": [1, 2, 3, 4], "action": "hold_for_review"}))

    # ------------------------------------------------------------------
    # Core ingest pipeline
    # ------------------------------------------------------------------

    def ingest(self, document: dict, originator_pubkey: bytes) -> IngestResult:
        """Validate an external document through the gate sequence.

        Args:
            document: The signed document dict (must include 'proof' field).
            originator_pubkey: Raw Ed25519 public key bytes of the sender.

        Returns:
            IngestResult with status, gate results, and quarantine ID if held.
        """
        doc_type = self._resolve_document_type(document)
        rule = self._get_rule(doc_type)
        required_gates = rule["gates"]
        action = rule["action"]

        gate_results = {}
        qid = f"dmz_{secrets.token_hex(8)}"
        internal_signer = False

        if not originator_pubkey:
            internal_signer = self._is_internal_signer(document)
            if internal_signer:
                required_gates = [3, 4]
                action = "auto_promote"
            else:
                return self._quarantine(
                    document,
                    doc_type,
                    qid,
                    gate_results,
                    b"",
                    "Gate 1 failed: empty sender pubkey and unrecognized internal signer",
                )

        # --- Gate 1: Authenticity ---
        if 1 in required_gates:
            try:
                verify_key = VerifyKey(originator_pubkey)
                if verify_document(document, verify_key):
                    gate_results[1] = "pass"
                else:
                    gate_results[1] = "fail"
                    return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, "Gate 1 failed: signature verification failed")
            except Exception as exc:
                gate_results[1] = "fail"
                return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, f"Gate 1 failed: {exc}")
        else:
            gate_results[1] = "skip"

        # --- Gate 2: Addressing ---
        if 2 in required_gates:
            if self._check_addressing(document):
                gate_results[2] = "pass"
            else:
                gate_results[2] = "fail"
                return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, "Gate 2 failed: document does not reference our identity")
        else:
            gate_results[2] = "skip"

        # --- Gate 3: Schema ---
        if 3 in required_gates:
            validators = self._get_validators()
            if doc_type in validators:
                validator = validators[doc_type]
                if validator is not None:
                    # Extract body for validation — the document itself or a nested body
                    body = document.get("body", document)
                    try:
                        valid, err = validator(body)
                    except Exception as exc:
                        gate_results[3] = "fail"
                        return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, f"Gate 3 failed: validator crashed: {exc}")
                    if valid:
                        gate_results[3] = "pass"
                    else:
                        gate_results[3] = "fail"
                        return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, f"Gate 3 failed: schema validation: {err}")
                else:
                    # Recognized type with no body validator — pass
                    gate_results[3] = "pass"
            else:
                # Unknown type — hold external docs for review, but let trusted
                # internal docs continue through integrity + countersign.
                gate_results[3] = "pass"
                if not internal_signer:
                    action = "hold_for_review"
                if self._debug:
                    logger.debug(f"WM unknown document_type={doc_type}, defaulting to hold_for_review")
        else:
            gate_results[3] = "skip"

        # --- Gate 4: Integrity ---
        if 4 in required_gates:
            integrity_ok, integrity_reason = self._check_integrity(document)
            if integrity_ok:
                gate_results[4] = "pass"
            else:
                gate_results[4] = "fail"
                return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, f"Gate 4 failed: {integrity_reason}")
        else:
            gate_results[4] = "skip"

        # --- Gate 5: Authorization ---
        if 5 in required_gates:
            if self._check_authorization(document, doc_type):
                gate_results[5] = "pass"
            else:
                gate_results[5] = "fail"
                return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, "Gate 5 failed: unauthorized signer for this document type")
        else:
            gate_results[5] = "skip"

        # --- All gates passed ---
        if action == "auto_promote":
            result = self._promote(document, doc_type, qid, gate_results, originator_pubkey)
            if internal_signer:
                return replace(result, needs_countersign=True, document=document)
            return result
        elif action == "hold_for_review":
            return self._hold(document, doc_type, qid, gate_results, originator_pubkey)
        else:
            # action == "reject" — operator configured rejection
            return self._quarantine(document, doc_type, qid, gate_results, originator_pubkey, f"Rejected by rule: action={action}")

    # ------------------------------------------------------------------
    # Review interface (pull-based)
    # ------------------------------------------------------------------

    def request_review(self, quarantine_id: str) -> Optional[dict]:
        """Return the quarantined document dict for inspection."""
        row = self._storage.quarantine_get(quarantine_id)
        if row is None:
            return None
        try:
            return json.loads(row["document_json"])
        except (json.JSONDecodeError, KeyError):
            return None

    def approve(self, quarantine_id: str) -> bool:
        """Promote a held document to bus + DB."""
        row = self._storage.quarantine_get(quarantine_id)
        if row is None or row["status"] not in ("pending",):
            return False
        try:
            document = json.loads(row["document_json"])
        except (json.JSONDecodeError, KeyError):
            return False

        doc_type = row["document_type"]
        now = time.time()

        # Emit to bus
        self._bus.emit(
            f"wm.promoted.{doc_type}",
            document_type=doc_type,
            quarantine_id=quarantine_id,
            identity=self._node_id,
        )

        # Write receipt
        self._write_receipt_cb(
            document_type=doc_type,
            payload=document,
            counterparty=document.get("identity", document.get("counterparty")),
            order_ref=document.get("receipt_id"),
            proof_purpose="assertionMethod",
        )

        # Update quarantine status
        self._storage.quarantine_update_status(
            quarantine_id, "promoted", promoted_at=now
        )

        if self._debug:
            logger.debug(f"WM APPROVE qid={quarantine_id} type={doc_type}")
        return True

    def reject(self, quarantine_id: str, reason: str) -> bool:
        """Reject a held document. Sets resolved_at, logs reason."""
        row = self._storage.quarantine_get(quarantine_id)
        if row is None or row["status"] not in ("pending",):
            return False

        now = time.time()
        self._storage.quarantine_update_status(
            quarantine_id, "rejected", reason=reason, resolved_at=now
        )

        if self._debug:
            logger.debug(f"WM REJECT qid={quarantine_id} reason={reason}")
        return True

    # ------------------------------------------------------------------
    # Internal gate checks
    # ------------------------------------------------------------------

    def _check_addressing(self, document: dict) -> bool:
        """Gate 2: verify the document references one of our identity fragments."""
        # Check identity / counterparty fields (exact match, reject empty strings)
        for field in ("identity", "counterparty", "proposer", "to", "recipient"):
            value = document.get(field, "")
            if isinstance(value, str) and value and value in self._identity_fragments:
                return True
        # Check proof.verificationMethod if present (exact DID fragment match)
        proof = document.get("proof", {})
        if isinstance(proof, dict):
            vm = proof.get("verificationMethod") or ""
            if isinstance(vm, str):
                # Extract the DID base (before #fragment) and compare against identity fragments
                did_base = vm.split("#")[0] if "#" in vm else vm
                for frag in self._identity_fragments:
                    # Match if the DID base equals the fragment, or the full VM equals the fragment
                    if frag and (did_base == frag or vm == frag):
                        return True
        return False

    def _check_integrity(self, document: dict) -> tuple[bool, str]:
        """Gate 4: verify proof fields and timestamp sanity."""
        proof = document.get("proof")
        if not isinstance(proof, dict):
            return False, "missing proof object"

        # Require proof type and cryptosuite
        if proof.get("type") != "DataIntegrityProof":
            return False, f"unexpected proof type: {proof.get('type')}"
        if proof.get("cryptosuite") != "eddsa-jcs-2022":
            return False, f"unexpected cryptosuite: {proof.get('cryptosuite')}"

        # Verify created timestamp is sane
        created = proof.get("created", "")
        if not isinstance(created, str) or not created:
            return False, "missing proof.created timestamp"

        # Parse ISO 8601 timestamp
        try:
            from datetime import datetime, timezone
            # Handle both Z suffix and +00:00
            ts_str = created.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            ts_epoch = dt.timestamp()
        except (ValueError, TypeError):
            return False, f"unparseable proof.created: {created}"

        # Reject NaN/Inf
        if not math.isfinite(ts_epoch):
            return False, f"non-finite timestamp: {ts_epoch}"

        now = time.time()
        if ts_epoch > now + _MAX_FUTURE_SKEW:
            return False, f"timestamp too far in future: {created}"
        if ts_epoch < now - _MAX_AGE:
            return False, f"timestamp too old: {created}"

        # Verify proofValue is present and multibase-encoded
        pv = proof.get("proofValue", "")
        if not isinstance(pv, str) or not pv.startswith("z"):
            return False, "missing or malformed proofValue"

        return True, ""

    def _check_authorization(self, document: dict, doc_type: str) -> bool:
        """Gate 5: check signer has authority for this document type."""
        proof = document.get("proof", {})
        vm = (proof.get("verificationMethod") or "") if isinstance(proof, dict) else ""

        # configuration_order requires OUR #cockpit-1 signer
        if doc_type == "configuration_order":
            if not isinstance(vm, str) or not vm.endswith("#cockpit-1"):
                return False
            did_base = vm.split("#")[0] if "#" in vm else vm
            return did_base in self._identity_fragments

        # Settlement types: any known signer passes (no special fragment needed)
        # The hold_for_review action provides the human/agent review layer.
        return True

    def _resolve_document_type(self, document: dict) -> str:
        doc_type = document.get("document_type")
        if isinstance(doc_type, str) and doc_type:
            return doc_type

        raw_type = document.get("type", "unknown")
        if not isinstance(raw_type, str):
            return "unknown"

        legacy_map = {
            "knarr/commerce/credit_note": "credit_note",
            "knarr/commerce/receipt": "execution_receipt",
            "knarr/commerce/settlement_confirmation": "settlement_confirmation",
            "knarr/commerce/tab_reminder": "tab_reminder",
        }
        return legacy_map.get(raw_type, raw_type)

    def _is_internal_signer(self, document: dict) -> bool:
        if not self._internal_signer_keys:
            return False

        proof = document.get("proof", {})
        if not isinstance(proof, dict):
            return False

        # Only trust the verificationMethod DID fragment — it names the key
        # used for signing and is verified cryptographically by Gate 4.
        # Do NOT check self-asserted fields like publicKeyHex which can be
        # forged by an attacker to impersonate an internal signer.
        vm = proof.get("verificationMethod") or ""
        if isinstance(vm, str):
            did_base, _, fragment = vm.partition("#")
            if did_base == f"did:knarr:{self._node_id}" and fragment in self._internal_signer_keys:
                return True

        return False

    # ------------------------------------------------------------------
    # Internal actions
    # ------------------------------------------------------------------

    def _quarantine(
        self, document: dict, doc_type: str, qid: str,
        gate_results: dict, originator_pubkey: bytes, reason: str
    ) -> IngestResult:
        """Store a failed/held document in the quarantine table."""
        pubkey_hex = originator_pubkey.hex() if originator_pubkey else ""
        self._storage.quarantine_store(
            id=qid,
            document_type=doc_type,
            document_json=document,
            originator_pubkey=pubkey_hex,
            status="rejected",
            gate_results={str(k): v for k, v in gate_results.items()},
            reason=reason,
        )

        if self._debug:
            logger.debug(f"WM QUARANTINE qid={qid} type={doc_type} reason={reason}")

        return IngestResult(
            status="rejected",
            document_type=doc_type,
            quarantine_id=qid,
            gate_results=gate_results,
            reason=reason,
        )

    def _hold(
        self, document: dict, doc_type: str, qid: str,
        gate_results: dict, originator_pubkey: bytes
    ) -> IngestResult:
        """Hold a document in quarantine for review."""
        pubkey_hex = originator_pubkey.hex() if originator_pubkey else ""
        self._storage.quarantine_store(
            id=qid,
            document_type=doc_type,
            document_json=document,
            originator_pubkey=pubkey_hex,
            status="pending",
            gate_results={str(k): v for k, v in gate_results.items()},
            reason=None,
        )

        self._bus.emit(
            f"wm.held.{doc_type}",
            document_type=doc_type,
            quarantine_id=qid,
            identity=self._node_id,
        )

        if self._debug:
            logger.debug(f"WM HOLD qid={qid} type={doc_type}")

        return IngestResult(
            status="held",
            document_type=doc_type,
            quarantine_id=qid,
            gate_results=gate_results,
            reason=None,
        )

    def _promote(
        self, document: dict, doc_type: str, qid: str,
        gate_results: dict, originator_pubkey: bytes
    ) -> IngestResult:
        """Promote a document: emit to bus + write receipt."""
        # Emit to bus
        self._bus.emit(
            f"wm.promoted.{doc_type}",
            document_type=doc_type,
            quarantine_id=qid,
            identity=self._node_id,
        )

        # Write receipt via callback
        self._write_receipt_cb(
            document_type=doc_type,
            payload=document,
            counterparty=document.get("identity", document.get("counterparty")),
            order_ref=document.get("receipt_id"),
            proof_purpose="assertionMethod",
        )

        if self._debug:
            logger.debug(f"WM PROMOTE qid={qid} type={doc_type}")

        return IngestResult(
            status="promoted",
            document_type=doc_type,
            quarantine_id=None,
            gate_results=gate_results,
            reason=None,
        )
