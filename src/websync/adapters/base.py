"""The Adapter protocol every tool integration must implement."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from websync.models import SyncItem


class SyncError(Exception):
    """Base class for all adapter failures."""

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        super().__init__(f"[{tool}] {message}")


class AuthError(SyncError):
    """Authentication/authorization failure (token expired, bad creds)."""


@runtime_checkable
class Adapter(Protocol):
    """Minimal contract the sync engine needs from any tool.

    Implementations must:
      * retry transient failures with backoff,
      * paginate every list endpoint,
      * set an HTTP timeout on every request,
      * re-authenticate once on 401 and retry.
    """

    tool_name: str

    def healthcheck(self) -> None:
        """Verify reachability/auth; raise :class:`SyncError` on failure."""
        ...

    def fetch_changed(self, whence: datetime) -> list[SyncItem]:
        """Return items changed since ``whence`` (first run: all in scope)."""
        ...

    def get(self, native_id: str) -> SyncItem:
        """Fetch one item by its tool-native ID."""
        ...

    def create(self, item: SyncItem) -> str:
        """Create an item; return its new native ID."""
        ...

    def update(self, native_id: str, changes: dict) -> None:
        """Apply a partial field update to an existing item."""
        ...
