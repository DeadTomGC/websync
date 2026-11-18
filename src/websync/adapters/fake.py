"""In-memory fake adapter.

Used by the engine test suite and by ``websync run --fake`` to exercise the
full pipeline without network access. Not registered for real configs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from websync.adapters.base import SyncError
from websync.models import SyncItem


class InMemoryAdapter:
    tool_name = "fake"

    def __init__(self) -> None:
        self._fields: dict[str, dict] = {}
        self._types: dict[str, str] = {}
        self._modified: dict[str, datetime] = {}
        self._created_at: dict[str, datetime] = {}
        self._next_id = 0

    # --- test helpers -------------------------------------------------

    def seed(
        self,
        native_id: str,
        type_: str,
        fields: dict,
        modified: datetime | None = None,
    ) -> str:
        now = datetime.now(UTC)
        self._fields[native_id] = dict(fields)
        self._types[native_id] = type_
        self._modified[native_id] = modified or now
        self._created_at[native_id] = now
        return native_id

    def fields_of(self, native_id: str) -> dict:
        return dict(self._fields[native_id])

    # --- Adapter protocol ---------------------------------------------

    def healthcheck(self) -> None:
        pass

    def fetch_changed(self, whence: datetime) -> list[SyncItem]:
        return [
            SyncItem(
                native_id=nid,
                type=self._types[nid],
                fields=dict(f),
                changed_at=self._modified[nid],
            )
            for nid, f in self._fields.items()
            if self._modified[nid] >= whence
        ]

    def get(self, native_id: str) -> SyncItem:
        if native_id not in self._fields:
            raise SyncError(self.tool_name, f"item {native_id!r} not found")
        return SyncItem(
            native_id=native_id,
            type=self._types[native_id],
            fields=dict(self._fields[native_id]),
            changed_at=self._modified[native_id],
        )

    def create(self, item: SyncItem) -> str:
        self._next_id += 1
        native_id = f"{self.tool_name}-{self._next_id}"
        self._fields[native_id] = dict(item.fields)
        self._types[native_id] = item.type
        now = datetime.now(UTC)
        self._modified[native_id] = now
        self._created_at[native_id] = now
        return native_id

    def update(self, native_id: str, changes: dict) -> None:
        if native_id not in self._fields:
            raise SyncError(self.tool_name, f"item {native_id!r} not found")
        self._fields[native_id].update(changes)
        self._modified[native_id] = datetime.now(UTC)
