import json
import uuid
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Union
from .models import NodeInfo, SkillSheet

@dataclass(frozen=True)
class Message:
    """Base class for all network messages."""
    type: str
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    public_key: str = ""    # hex-encoded 32-byte Ed25519 public key (64 hex chars)
    signature: str = ""     # hex-encoded 64-byte Ed25519 signature (128 hex chars)

    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary."""
        return asdict(self)

@dataclass(frozen=True)
class JoinRequest(Message):
    """Request to join the network."""
    node_id: str = ""
    host: str = ""
    port: int = 0
    ephemeral: bool = False
    type: str = "JOIN_REQUEST"

SIGNATURE_EXCLUDED_FIELDS = {"signature", "hops", "ephemeral", "load", "sidecar_port", "version", "min_protocol_version", "mode", "peer_count", "batch_seq", "encryption_key", "receipt", "provider_host", "provider_port", "wallet", "has_wallet", "jurisdiction"}

@dataclass(frozen=True)
class JoinResponse(Message):
    """Response to a join request containing peer list."""
    peers: List[Dict[str, Any]] = field(default_factory=list)
    type: str = "JOIN_RESPONSE"

@dataclass(frozen=True)
class Announce(Message):
    """Announce a skill to peers.

    v0.38.0 A3.1: wallet field replaced by has_wallet boolean.
    Wallet address is no longer broadcast in the DHT — it is served via
    the punchhole wallet.address object (peer-tier access).
    """
    node_id: str = ""
    skill_key: str = ""
    skill_sheet: Dict[str, Any] = field(default_factory=dict)
    hops: int = 0
    sidecar_port: int = 0  # 0 = no sidecar available
    encryption_key: str = ""
    wallet: str = ""              # DEPRECATED v0.38.0 — kept for wire compatibility
    has_wallet: bool = False      # v0.38.0: True if node has a hot wallet
    provider_host: str = ""  # Provider's advertise host (preserved through gossip)
    provider_port: int = 0   # Provider's protocol port (preserved through gossip)
    jurisdiction: str = ""   # e.g. "eu.ch", "eu.de" — for DHT-computed groups
    type: str = "ANNOUNCE"

@dataclass(frozen=True)
class Query(Message):
    """Query for skills by name or tag."""
    query_type: str = "" # "name" or "tag"
    value: str = ""
    type: str = "QUERY"

@dataclass(frozen=True)
class QueryResponse(Message):
    """Response to a skill query."""
    results: List[Dict[str, Any]] = field(default_factory=list)
    type: str = "QUERY_RESPONSE"

@dataclass(frozen=True)
class Deregister(Message):
    """Remove a skill from the network."""
    node_id: str = ""
    skill_key: str = ""
    hops: int = 0
    type: str = "DEREGISTER"

@dataclass(frozen=True)
class Heartbeat(Message):
    """Keep-alive exchange."""
    node_id: str = ""
    timestamp: float = 0.0
    version: str = ""  # sender's knarr version (e.g. "0.6.0")
    min_protocol_version: str = ""  # minimum version to participate (set by bootstrap)
    type: str = "HEARTBEAT"

@dataclass(frozen=True)
class Ack(Message):
    """Acknowledge a message."""
    status: str = "" # "ok" or "error"
    error_detail: Optional[str] = None
    type: str = "ACK"

@dataclass(frozen=True)
class TaskRequest(Message):
    """Request task execution from a provider."""
    task_id: str = ""
    requester_node_id: str = ""
    requester_host: str = ""
    requester_port: int = 0
    skill_name: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30000
    mode: str = "sync"  # "sync" (default) or "async"
    type: str = "TASK_REQUEST"

@dataclass(frozen=True)
class TaskStatus(Message):
    """Update on task status (accepted/rejected/queued)."""
    task_id: str = ""
    status: str = ""  # "accepted", "rejected", or "queued"
    reason: str = ""  # optional, for rejection
    position: int = 0  # queue position when status="queued"
    type: str = "TASK_STATUS"

@dataclass(frozen=True)
class TaskResult(Message):
    """Result of a task execution."""
    task_id: str = ""
    status: str = ""  # "completed" or "failed"
    output_data: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] = field(default_factory=dict)
    receipt: str = ""
    type: str = "TASK_RESULT"

@dataclass(frozen=True)
class SyncRequest(Message):
    """Request skill registry sync from a peer."""
    since: float = 0.0
    type: str = "SYNC_REQUEST"

@dataclass(frozen=True)
class SyncResponse(Message):
    """Response with skill registry entries."""
    skills: List[Dict[str, Any]] = field(default_factory=list)
    type: str = "SYNC_RESPONSE"

@dataclass(frozen=True)
class Warn(Message):
    """Backpressure warning — advises sender to slow down."""
    delay_ms: int = 0
    reason: str = ""
    pressure_pct: int = 0
    type: str = "WARN"

@dataclass(frozen=True)
class Blocked(Message):
    """Hard block notification — sender is banned for duration."""
    reason: str = ""
    duration_minutes: int = 0
    type: str = "BLOCKED"

@dataclass(frozen=True)
class MailSync(Message):
    """Deliver a batch of mail items to a peer."""
    sender_node_id: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    batch_seq: int = 0
    type: str = "MAIL_SYNC"

@dataclass(frozen=True)
class MailAck(Message):
    """Acknowledge receipt of mail items."""
    sender_node_id: str = ""
    ack_seq: int = 0
    item_ids: List[str] = field(default_factory=list)
    type: str = "MAIL_ACK"

@dataclass(frozen=True)
class PluginMessage(Message):
    """Generic envelope for inter-plugin communication between nodes."""
    node_id: str = ""
    plugin_name: str = ""    # target plugin (e.g. "knarr-kademlia")
    action: str = ""         # plugin-specific action (e.g. "FIND_NODE")
    payload: str = ""        # JSON-encoded plugin-specific data
    type: str = "PLUGIN_MESSAGE"

@dataclass(frozen=True)
class MailPullReq(Message):
    """Request pending mail from a peer (Tier 2 pull)."""
    requester_node_id: str = ""
    since_timestamp: float = 0.0
    type: str = "MAIL_PULL_REQ"

@dataclass(frozen=True)
class MailPullResp(Message):
    """Response with pending mail items (Tier 2 pull)."""
    sender_node_id: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    type: str = "MAIL_PULL_RESP"

@dataclass(frozen=True)
class MailPullAck(Message):
    """Acknowledge receipt of pulled mail items."""
    requester_node_id: str = ""
    item_ids: List[str] = field(default_factory=list)
    type: str = "MAIL_PULL_ACK"


def serialize_message(msg: Message) -> bytes:
    """Serializes a message to JSON bytes."""
    return json.dumps(msg.to_dict()).encode("utf-8")

def deserialize_message(data: bytes) -> Message:
    """Deserializes JSON bytes to a Message object."""
    payload = json.loads(data.decode("utf-8"))
    msg_type = payload.get("type")
    
    # Map type to class
    type_map = {
        "JOIN_REQUEST": JoinRequest,
        "JOIN_RESPONSE": JoinResponse,
        "ANNOUNCE": Announce,
        "QUERY": Query,
        "QUERY_RESPONSE": QueryResponse,
        "DEREGISTER": Deregister,
        "HEARTBEAT": Heartbeat,
        "ACK": Ack,
        "TASK_REQUEST": TaskRequest,
        "TASK_STATUS": TaskStatus,
        "TASK_RESULT": TaskResult,
        "SYNC_REQUEST": SyncRequest,
        "SYNC_RESPONSE": SyncResponse,
        "WARN": Warn,
        "BLOCKED": Blocked,
        "MAIL_SYNC": MailSync,
        "MAIL_ACK": MailAck,
        "PLUGIN_MESSAGE": PluginMessage,
        "MAIL_PULL_REQ": MailPullReq,
        "MAIL_PULL_RESP": MailPullResp,
        "MAIL_PULL_ACK": MailPullAck,
    }
    
    cls = type_map.get(msg_type)
    if not cls:
        raise ValueError(f"Unknown message type: {msg_type}")
    
    # SA-07 filter: only pass known fields to dataclass constructor
    known_fields = cls.__dataclass_fields__
    filtered = {k: v for k, v in payload.items() if k in known_fields}
    return cls(**filtered)

def sign_message(msg: Message, signing_key: Any) -> Message:
    """Signs a message and returns a new message with public_key and signature set."""
    # signing_key is expected to be a nacl.signing.SigningKey
    public_key_hex = signing_key.verify_key.encode().hex()

    # Build dict, strip excluded fields for canonicalization
    d = msg.to_dict()
    d["public_key"] = public_key_hex
    d["signature"] = ""
    for field in SIGNATURE_EXCLUDED_FIELDS:
        if field != "signature":
            d.pop(field, None)

    # Canonical JSON
    canonical = json.dumps(d, sort_keys=True, separators=(',', ':')).encode('utf-8')

    # Sign
    signed = signing_key.sign(canonical)
    signature_hex = signed.signature.hex()

    # Return new message with fields set
    d_full = msg.to_dict()
    d_full["public_key"] = public_key_hex
    d_full["signature"] = signature_hex

    cls = type(msg)
    known_fields = cls.__dataclass_fields__
    filtered = {k: v for k, v in d_full.items() if k in known_fields}
    return cls(**filtered)

def verify_message(msg: Message) -> bool:
    """Verifies the signature on a message. Returns True if valid."""
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError

    if not msg.public_key or not msg.signature:
        return False

    try:
        # Reconstruct the canonical payload (strip excluded fields for compat)
        d = msg.to_dict()
        d["signature"] = ""
        for field in SIGNATURE_EXCLUDED_FIELDS:
            if field != "signature":
                d.pop(field, None)

        canonical = json.dumps(d, sort_keys=True, separators=(',', ':')).encode('utf-8')

        # Verify
        verify_key = VerifyKey(bytes.fromhex(msg.public_key))
        verify_key.verify(canonical, bytes.fromhex(msg.signature))
        return True
    except (BadSignatureError, ValueError, Exception):
        return False

def verify_node_id(msg: Message) -> bool:
    """Verifies that node_id matches SHA-256(public_key) for messages that carry node_id."""
    if not hasattr(msg, 'node_id') or not msg.node_id:
        return True  # Message doesn't carry node_id
    if not msg.public_key:
        return False
    expected = hashlib.sha256(bytes.fromhex(msg.public_key)).hexdigest()
    return msg.node_id == expected
