from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set

@dataclass(frozen=True)
class NodeInfo:
    """Information about a node in the Knarr network."""
    node_id: str
    host: str
    port: int

    def __post_init__(self):
        """Normalize host to lowercase."""
        object.__setattr__(self, "host", self.host.lower())

@dataclass(frozen=True)
class SkillSheet:
    """Descriptor for an agent's capability."""
    name: str
    version: str
    description: str
    tags: List[str]
    input_schema: Dict[str, str]
    output_schema: Dict[str, str]
    input_schema_full: Optional[Dict[str, Any]] = None
    input_spec: Optional[str] = None               # sidecar asset hash for rich JSON Schema
    uri: Optional[str] = None                       # knarr:///category/name@version
    jurisdiction: Optional[List[str]] = None        # compliance routing ["eu.no", "eu.ch"]
    price: float = 1.0
    max_input_size: int = 65536

    def __post_init__(self):
        """Normalize name and tags to lowercase and strip whitespace."""
        object.__setattr__(self, "name", self.name.strip().lower())
        object.__setattr__(self, "tags", [t.strip().lower() for t in self.tags])

    def to_dict(self) -> dict:
        """Convert skill sheet to a dictionary."""
        d = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": self.tags,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }
        if self.input_schema_full is not None:
            d["input_schema_full"] = self.input_schema_full
        if self.input_spec is not None:
            d["input_spec"] = self.input_spec
        if self.uri is not None:
            d["uri"] = self.uri
        if self.jurisdiction is not None:
            d["jurisdiction"] = self.jurisdiction
        if self.price != 1.0:
            d["price"] = self.price
        if self.max_input_size != 65536:
            d["max_input_size"] = self.max_input_size
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SkillSheet":
        """Create a SkillSheet from a dictionary."""
        return cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            tags=data["tags"],
            input_schema=data["input_schema"],
            output_schema=data["output_schema"],
            input_schema_full=data.get("input_schema_full"),
            input_spec=data.get("input_spec"),
            uri=data.get("uri"),
            jurisdiction=data.get("jurisdiction"),
            price=data.get("price", 1.0),
            max_input_size=data.get("max_input_size", 65536),
        )

@dataclass(frozen=True)
class Task:
    """Represents a task lifecycle."""
    task_id: str
    skill_name: str
    requester_node_id: str
    provider_node_id: str
    status: str  # submitted, accepted, rejected, completed, failed
    input_data: Dict[str, Any]
    output_data: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    timeout_ms: int = 30000

    def to_dict(self) -> dict:
        """Convert task to dictionary."""
        return {
            "task_id": self.task_id,
            "skill_name": self.skill_name,
            "requester_node_id": self.requester_node_id,
            "provider_node_id": self.provider_node_id,
            "status": self.status,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "timeout_ms": self.timeout_ms
        }

@dataclass(frozen=True)
class LedgerEntry:
    """Bilateral balance with a specific peer."""
    peer_public_key: str
    balance: float = 0.0
    tasks_provided: int = 0
    tasks_consumed: int = 0
    first_seen: float = 0.0
    last_updated: float = 0.0
    prepaid: float = 0.0
    pub_tab: float = 0.0
    soft_limit: float = 0.0
    hard_limit: float = 0.0
    held_balance: float = 0.0
    credit_limit: float = 0.0
    trust: float = 0.0

@dataclass(frozen=True)
class Policy:
    """Provider-side policy configuration."""
    initial_credit: float = 3.0
    min_balance: float = -10.0
    tit_for_tat: bool = False

@dataclass
class GroupPolicy:
    """Policy for a named group of peers."""
    name: str
    members: Set[str]            # public keys (hex)
    members_file: Optional[str]  # path to file with one pubkey per line
    initial_credit: float
    min_balance: float

@dataclass
class SkillPolicy:
    """Policy override for a specific skill."""
    skill_name: str
    initial_credit: Optional[float]   # None = inherit from group/default
    min_balance: Optional[float]      # None = inherit from group/default
