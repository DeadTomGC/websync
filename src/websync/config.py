"""Pydantic models for project mapping YAML configs and their loader.

This is the validated schema described in the plan's mapping-configuration
spec. Every config is loaded *through* these models — never raw YAML.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldMap(BaseModel):
    """Mapping of one normalized field to per-tool native fields."""

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    format: str | None = None  # html | plain | markdown
    value_map: dict[str, str | int] | None = None  # source-tool value -> target-tool value
    transform: str | None = None  # named hook in websync.transforms
    relation: str | None = None  # e.g. parent_child; resolved via id_map


class ScopeConfig(BaseModel):
    """Limits which items a link looks at, in tool-native vocabulary."""

    model_config = ConfigDict(extra="forbid")

    project: str | int
    item_type_filter: list[str] | None = None


class LinkConfig(BaseModel):
    """One synchronized pair of item types between two tools."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source: str
    target: str
    types: dict[str, str]
    scope: dict[str, ScopeConfig] = Field(min_length=1, max_length=2)
    fields: dict[str, FieldMap] = Field(default_factory=dict)
    direction: str = "bidirectional"  # bidirectional | source_to_target | target_to_source
    conflict_strategy: str = (
        "skip_and_report"  # newest_wins | source_wins | target_wins | skip_and_report
    )
    create_missing: bool = True
    delete_behavior: str = "skip"  # skip | close_counterpart

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        allowed = {"bidirectional", "source_to_target", "target_to_source"}
        if v not in allowed:
            raise ValueError(f"direction must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("conflict_strategy")
    @classmethod
    def _check_conflict_strategy(cls, v: str) -> str:
        allowed = {"newest_wins", "source_wins", "target_wins", "skip_and_report"}
        if v not in allowed:
            raise ValueError(f"conflict_strategy must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("delete_behavior")
    @classmethod
    def _check_delete_behavior(cls, v: str) -> str:
        allowed = {"skip", "close_counterpart"}
        if v not in allowed:
            raise ValueError(f"delete_behavior must be one of {sorted(allowed)}, got {v!r}")
        return v


class ProgramConfig(BaseModel):
    """Root object of a project YAML config file."""

    model_config = ConfigDict(extra="forbid")

    program: str
    links: list[LinkConfig] = Field(min_length=1)

    def link(self, name: str) -> LinkConfig:
        matches = [link for link in self.links if link.name == name]
        if not matches:
            raise KeyError(f"no link named {name!r} in program {self.program!r}")
        return matches[0]


def load_config(path: Path | str) -> ProgramConfig:
    """Load and validate a project config; raises pydantic ValidationError."""
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return ProgramConfig.model_validate(raw)
