"""Lightweight event emitter for AG-UI events. Thread-safe."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List


class EventEmitter:
    """
    Lightweight event emitter for AG-UI events.

    Known types: RunStarted, RunFinished, TextMessageStart, TextMessageDelta,
    TextMessageEnd, ToolCallStart, ToolCallEnd, SkillExecuteInstruction,
    StateSnapshotEvent, Error.
    Special types: "event" (all events), "done" (RunFinished), "error" (Error or exception).
    """

    def __init__(self) -> None:
        self._listeners: Dict[str, List[Callable[..., Any]]] = {}
        self._lock = threading.Lock()

    def on(self, event_type: str, handler: Callable[..., Any]) -> "EventEmitter":
        """Register a listener for a specific event type."""
        with self._lock:
            self._listeners.setdefault(event_type, []).append(handler)
        return self

    def off(self, event_type: str, handler: Callable[..., Any]) -> "EventEmitter":
        """Remove a specific listener."""
        with self._lock:
            handlers = self._listeners.get(event_type)
            if handlers:
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass
        return self

    def remove_all_listeners(self, event_type: str | None = None) -> "EventEmitter":
        """Remove all listeners for a given event type, or all if None."""
        with self._lock:
            if event_type is None:
                self._listeners.clear()
            else:
                self._listeners.pop(event_type, None)
        return self

    def emit(self, event_type: str, *args: Any) -> None:
        """Emit an event to all registered listeners."""
        with self._lock:
            handlers = list(self._listeners.get(event_type, []))
        for handler in handlers:
            try:
                handler(*args)
            except Exception:
                pass  # swallow listener exceptions
