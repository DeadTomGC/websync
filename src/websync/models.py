"""Core tool-agnostic data models.

Every adapter normalizes its tool's items into :class:`SyncItem`; the sync
engine operates exclusively on these models.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    """Which side of a link an item belongs to (source or target)."""

    SOURCE = "source"
    TARGET = "target"


class SyncItem(BaseModel):
    """Normalized item fetched from (or written to) a tool.

    ``native_id`` / ``type`` are in the tool's own vocabulary; ``fields``
    uses the *normalized* field names defined by the link config.
    """

    model_config = ConfigDict(frozen=True)

    native_id: str
    type: str
    fields: dict[str, Any] = Field(default_factory=dict)
    changed_at: datetime | None = None
    version: str | None = None


class Change(BaseModel):
    """A single change the engine intends to apply (or applied)."""

    action: str  # "create" | "update" | "close" | "no-op"
    link_name: str
    side: Side
    native_id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class Conflict(BaseModel):
    """An item that changed on both sides since the last sync."""

    link_name: str
    native_id: str
    strategy: str  # conflict strategy chosen from config
    detail: dict[str, Any] = Field(default_factory=dict)


class RunStats(BaseModel):
    """Summary counters for one sync run (written to state + report)."""

    created: int = 0
    updated: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: int = 0
