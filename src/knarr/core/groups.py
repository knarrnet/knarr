"""GroupEngine — universal membership resolution interface."""
from typing import Dict, List, Protocol, Set
import logging

log = logging.getLogger(__name__)


class GroupEngine(Protocol):
    """Core interface. Two methods. Groups are pure named sets.

    is_member() is called on the firewall hot path (every inbound message).
    Both methods MUST be synchronous and read from in-memory cache only.
    """

    def is_member(self, item_id: str, group: str) -> bool:
        """Synchronous O(1) membership check. Hot path safe."""
        ...

    def get_groups(self, item_id: str) -> List[str]:
        """Return all groups containing this item. Synchronous."""
        ...


class DefaultGroupEngine:
    """Fallback when no groups plugin is loaded. Reads static TOML config."""

    def __init__(self, config_groups: Dict[str, Set[str]]):
        self._groups = config_groups

    def is_member(self, item_id: str, group: str) -> bool:
        return item_id in self._groups.get(group, set())

    def get_groups(self, item_id: str) -> List[str]:
        return [g for g, members in self._groups.items() if item_id in members]
