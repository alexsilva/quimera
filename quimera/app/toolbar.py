"""Toolbar management for QuimeraApp."""

import os
import threading
from pathlib import Path
from typing import Dict, Tuple, Any


class ToolbarManager:
    """Manages toolbar state and related functionality."""

    def __init__(self, threads: int = 1):
        self._parallel_toolbar_lock = threading.Lock()
        self._parallel_toolbar_state = {
            "active": 0,
            "queued": 0,
            "capacity": max(0, threads),
            "active_agents": (),
        }
        self._toolbar_bug_count_cache = {"session_id": "", "count": 0, "ts": 0.0}
        self._toolbar_bug_count_ttl_sec = 1.0

    def _get_parallel_toolbar_state(self) -> dict[str, object]:
        """Return a copy of the parallelism state from the toolbar."""
        with self._parallel_toolbar_lock:
            return dict(self._parallel_toolbar_state)

    def _set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Update the parallelism snapshot displayed on the toolbar."""
        with self._parallel_toolbar_lock:
            if active is not None:
                self._parallel_toolbar_state["active"] = max(0, int(active))
            if queued is not None:
                self._parallel_toolbar_state["queued"] = max(0, int(queued))
            if capacity is not None:
                self._parallel_toolbar_state["capacity"] = max(0, int(capacity))
            if active_agents is not None:
                self._parallel_toolbar_state["active_agents"] = tuple(active_agents)

    @property
    def toolbar_bug_count_cache(self) -> dict:
        """Get the toolbar bug count cache."""
        return self._toolbar_bug_count_cache

    @toolbar_bug_count_cache.setter
    def toolbar_bug_count_cache(self, value: dict) -> None:
        """Set the toolbar bug count cache."""
        self._toolbar_bug_count_cache = value

    @property
    def toolbar_bug_count_ttl_sec(self) -> float:
        """Get the toolbar bug count TTL."""
        return self._toolbar_bug_count_ttl_sec

    @toolbar_bug_count_ttl_sec.setter
    def toolbar_bug_count_ttl_sec(self, value: float) -> None:
        """Set the toolbar bug count TTL."""
        self._toolbar_bug_count_ttl_sec = value