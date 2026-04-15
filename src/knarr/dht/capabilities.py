from __future__ import annotations

from typing import Any, Callable

_MISSING = object()


class CapabilityRegistry:
    """Registry of lazily-read capabilities exposed by plugins or core seams."""

    def __init__(self):
        self._entries: dict[str, tuple[Callable[[], Any], Any]] = {}

    def register(self, name: str, getter: Callable[[], Any], default: Any = None) -> None:
        self._entries[str(name)] = (getter, default)

    def get(self, name: str, default: Any = _MISSING) -> Any:
        entry = self._entries.get(str(name))
        if entry is None:
            return None if default is _MISSING else default
        getter, registered_default = entry
        effective_default = registered_default if default is _MISSING else default
        try:
            value = getter()
        except Exception:
            return effective_default
        return effective_default if value is None else value

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))
