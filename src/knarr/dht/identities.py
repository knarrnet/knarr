"""E-03: IdentityRegistry — maps identity node_id -> Identity handler.

Each Identity holds its own signing key, storage, vault, bus, skill registrations,
and plugin set. The registry is the single source of truth for which identities
this node is hosting.

Single-identity mode: exactly one Identity registered under the node's own node_id.
Multi-identity mode: N identities, each with their own keys/storage/bus.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Identity:
    """A single hosted identity on this node.

    Attributes:
        name          Human-readable name (from config, e.g. "alice").
        node_id       SHA256(public_key) — this identity's network address.
        signing_key   Private signing key object (nacl.signing.SigningKey or compatible).
        storage       Storage instance for this identity (separate DB).
        vault         Vault instance for this identity's secrets.
        bus           EventBus scoped to this identity.
        skills        Dict mapping skill_name -> handler.
        plugins       Set of plugin names active for this identity.
        data_dir      Path to this identity's data directory.
    """

    def __init__(
        self,
        name: str,
        node_id: str,
        signing_key=None,
        storage=None,
        vault=None,
        bus=None,
        skills: Optional[Dict[str, Any]] = None,
        plugins: Optional[set] = None,
        data_dir: Optional[str] = None,
        debug: bool = False,
        public_key_hex: str = "",
    ):
        self.name = name
        self.node_id = node_id
        self.signing_key = signing_key
        self.storage = storage
        self.vault = vault
        self.bus = bus
        self.skills: Dict[str, Any] = skills or {}
        self.plugins: set = plugins or set()
        self.data_dir = data_dir
        self.public_key_hex = public_key_hex
        self._debug = debug

    def register_skill(self, skill_name: str, handler: Any = True) -> None:
        """Register a skill handler for this identity."""
        self.skills[skill_name] = handler
        if self._debug:
            logger.info(f"IDENTITY_SKILL_REGISTERED name={self.name} skill={skill_name}")

    def deregister_skill(self, skill_name: str) -> bool:
        """Remove a skill handler. Returns True if it existed."""
        existed = skill_name in self.skills
        self.skills.pop(skill_name, None)
        if self._debug and existed:
            logger.info(f"IDENTITY_SKILL_DEREGISTERED name={self.name} skill={skill_name}")
        return existed

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            "name": self.name,
            "node_id": self.node_id,
            "public_key_hex": self.public_key_hex,
            "skill_count": len(self.skills),
            "skills": list(self.skills.keys()),
            "data_dir": self.data_dir or "",
        }

    def __repr__(self) -> str:
        return f"Identity(name={self.name!r}, node_id={self.node_id[:16]!r}...)"


class IdentityRegistry:
    """Maps identity node_id -> Identity handler.

    Includes skill-to-identity routing (B's _skill_to_identity pattern).
    Backed by a dict. Thread-safe reads; mutations from single async context.

    Usage::

        registry = IdentityRegistry(default_node_id="aa...aa")
        registry.register(Identity(name="default", node_id="aa...aa", ...))

        identity = registry.resolve("aa...aa")
        identity = registry.default  # always the default identity
        identity = registry.identity_for_skill("llm/chat")  # skill routing
    """

    def __init__(self, default_node_id: str = "", debug: bool = False):
        self._identities: Dict[str, Identity] = {}
        self._name_to_id: Dict[str, str] = {}
        self._skill_to_identity: Dict[str, str] = {}  # skill_key -> node_id
        self._default_node_id = default_node_id
        self._debug = debug

    def register(self, identity: Identity) -> None:
        """Register an identity. The first registered identity becomes the default
        if no default_node_id was set at construction time.
        """
        self._identities[identity.node_id] = identity
        self._name_to_id[identity.name] = identity.node_id
        if not self._default_node_id:
            self._default_node_id = identity.node_id
        # Map skills to this identity
        for skill_name in identity.skills:
            self._skill_to_identity[str(skill_name)] = identity.node_id
        if self._debug:
            logger.info(
                "IDENTITY_REGISTERED name=%s node_id=%s skills=%d default=%s",
                identity.name, identity.node_id[:16],
                len(identity.skills), identity.node_id == self._default_node_id,
            )

    def unregister(self, node_id: str) -> Optional[Identity]:
        """Remove an identity by node_id. Returns the removed Identity or None."""
        identity = self._identities.pop(node_id, None)
        if identity is None:
            return None
        self._name_to_id.pop(identity.name, None)
        # Remove skill mappings
        for skill in identity.skills:
            self._skill_to_identity.pop(skill, None)
        # If we removed the default, pick next
        if self._default_node_id == node_id:
            self._default_node_id = next(iter(self._identities), "")
        return identity

    def resolve(self, node_id: str) -> Optional[Identity]:
        """Look up an identity by node_id. Returns None if not found."""
        return self._identities.get(node_id)

    def resolve_by_name(self, name: str) -> Optional[Identity]:
        """Look up an identity by human-readable name."""
        nid = self._name_to_id.get(name)
        if nid:
            return self._identities.get(nid)
        return None

    @property
    def default(self) -> Optional[Identity]:
        """The default identity (first registered, or the one with default_node_id)."""
        return self._identities.get(self._default_node_id)

    @property
    def default_node_id(self) -> str:
        """Return the default identity's node_id."""
        return self._default_node_id

    def identity_for_skill(self, skill_name: str) -> Optional[Identity]:
        """Find which identity serves a given skill."""
        node_id = self._skill_to_identity.get(str(skill_name))
        if not node_id:
            return None
        return self._identities.get(node_id)

    def map_skill(self, skill_name: str, node_id: str) -> None:
        """Map a skill to an identity node_id."""
        if node_id not in self._identities:
            raise KeyError(node_id)
        self._skill_to_identity[str(skill_name)] = node_id
        self._identities[node_id].register_skill(skill_name)

    def unmap_skill(self, skill_name: str) -> None:
        """Remove a skill mapping."""
        node_id = self._skill_to_identity.pop(str(skill_name), "")
        if node_id and node_id in self._identities:
            self._identities[node_id].deregister_skill(skill_name)

    @property
    def all(self) -> List[Identity]:
        """Return all registered identities."""
        return list(self._identities.values())

    def identities(self) -> List[Identity]:
        """Return all registered identities (method form)."""
        return list(self._identities.values())

    def node_ids(self) -> List[str]:
        """Return all registered identity node_ids."""
        return list(self._identities.keys())

    def list_identities(self) -> List[dict]:
        """Return list of identity dicts for API."""
        return [ident.to_dict() for ident in self._identities.values()]

    def __len__(self) -> int:
        return len(self._identities)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._identities

    def __repr__(self) -> str:
        names = [i.name for i in self._identities.values()]
        return f"IdentityRegistry(identities={names})"
