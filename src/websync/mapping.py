"""Config-driven field/type translation between two tools.

All translation goes ``tool native -> normalized -> tool native`` — never
directly between tools. The normalized field names are the keys of the link's
``fields`` mapping; each side declares which native field carries it.

Phase 1 semantics:
- ``value_map`` maps source-tool values to target-tool values; the reverse
  direction uses an inverted map (ambiguous folds raise on use).
- ``format`` names the field's canonical (normalized) format. Optional
  ``source_format``/``target_format`` declare a side's native format; without
  them no conversion happens.
- ``transform`` hooks (websync.transforms) apply only on the
  source -> normalized direction.
"""

from __future__ import annotations

from dataclasses import dataclass

from websync import transforms
from websync.config import LinkConfig
from websync.formats import KNOWN_FORMATS, convert_text
from websync.models import Side, SyncItem


class MappingError(Exception):
    """A value or type could not be translated (recorded as a conflict)."""


class UnmappedValue(MappingError):
    def __init__(self, field: str, value) -> None:
        self.field = field
        self.value = value
        super().__init__(f"no value_map entry for {field}={value!r}")


class UnmappedType(MappingError):
    def __init__(self, type_: str, side: Side) -> None:
        super().__init__(f"item type {type_!r} is not mapped for the {side.value} side")


class AmbiguousReverseType(MappingError):
    def __init__(self, target_type: str) -> None:
        super().__init__(
            f"type {target_type!r} folds from multiple source types "
            f"and no reverse_types entry resolves it"
        )


@dataclass
class _ResolvedField:
    normalized: str
    source_native: str
    target_native: str
    canonical_format: str | None
    source_format: str | None
    target_format: str | None
    forward: dict | None  # source value -> target value
    reverse: dict | None  # target value -> source value (None if ambiguous)
    transform: str | None


class Mapper:
    """Pure, stateless translator for one link's field/type mappings."""

    def __init__(self, link: LinkConfig) -> None:
        self.link = link
        self.types: dict[str, str] = dict(link.types)
        # Reverse type map: target type -> source type; track ambiguity.
        self._reverse_types: dict[str, str] = {}
        self._ambiguous_reverse: set[str] = set()
        for src_t, tgt_t in self.types.items():
            if tgt_t in self._reverse_types and self._reverse_types[tgt_t] != src_t:
                self._ambiguous_reverse.add(tgt_t)
            else:
                self._reverse_types[tgt_t] = src_t
        if link.reverse_types:
            for tgt_t, src_t in link.reverse_types.items():
                self._reverse_types[tgt_t] = src_t
                self._ambiguous_reverse.discard(tgt_t)

        self._fields: dict[str, _ResolvedField] = {}
        for name, fm in link.fields.items():
            if fm.format is not None and fm.format not in KNOWN_FORMATS:
                raise ValueError(f"field {name!r}: format must be one of {sorted(KNOWN_FORMATS)}")
            forward = dict(fm.value_map) if fm.value_map else None
            reverse = None
            if forward is not None:
                reverse = {}
                for src_v, tgt_v in forward.items():
                    if tgt_v in reverse and reverse[tgt_v] != src_v:
                        reverse = None  # ambiguous: disable reverse lookup
                        break
                    reverse[tgt_v] = src_v
            if fm.transform is not None and fm.transform not in transforms.known_transforms():
                raise ValueError(
                    f"field {name!r}: unknown transform {fm.transform!r} "
                    f"(known: {transforms.known_transforms()})"
                )
            self._fields[name] = _ResolvedField(
                normalized=name,
                source_native=fm.source,
                target_native=fm.target,
                canonical_format=fm.format,
                source_format=fm.source_format,
                target_format=fm.target_format,
                forward=forward,
                reverse=reverse,
                transform=fm.transform,
            )

    @classmethod
    def for_link(cls, link: LinkConfig) -> Mapper:
        return cls(link)

    # ------------------------------------------------------------------ types

    def has_source_type(self, source_type: str) -> bool:
        return source_type in self.types

    def target_type_for(self, source_type: str) -> str:
        return self.types[source_type]

    def source_type_for(self, target_type: str) -> str:
        """Reverse fold; raises UnmappedType / AmbiguousReverseType."""
        if target_type in self._ambiguous_reverse:
            raise AmbiguousReverseType(target_type)
        if target_type not in self._reverse_types:
            raise UnmappedType(target_type, Side.TARGET)
        return self._reverse_types[target_type]

    # ------------------------------------------------------------------ fields

    def _native_name(self, rf: _ResolvedField, side: Side) -> str:
        return rf.source_native if side == Side.SOURCE else rf.target_native

    def to_normalized(self, item: SyncItem, side: Side) -> dict:
        """Translate a tool-native SyncItem into a normalized field dict.

        Raises UnmappedValue when a mapped enumerated value has no entry.
        """
        out: dict = {}
        for rf in self._fields.values():
            native = self._native_name(rf, side)
            if native not in item.fields:
                continue
            value = item.fields[native]
            if side == Side.SOURCE:
                if rf.transform:
                    value = transforms.apply_transform(rf.transform, value)
                if rf.source_format and rf.canonical_format:
                    value = convert_text(value, rf.source_format, rf.canonical_format)
            else:
                if rf.forward is not None:
                    # Target-side value arrives in target vocab; reverse it to
                    # source vocab (the normalized vocabulary).
                    if rf.reverse is None or value not in rf.reverse:
                        raise UnmappedValue(rf.normalized, value)
                    value = rf.reverse[value]
                if rf.target_format and rf.canonical_format:
                    value = convert_text(value, rf.target_format, rf.canonical_format)
            out[rf.normalized] = value
        return out

    def from_normalized(self, normalized: dict, side: Side) -> dict:
        """Translate a normalized field dict into tool-native fields."""
        out: dict = {}
        for rf in self._fields.values():
            if rf.normalized not in normalized:
                continue
            value = normalized[rf.normalized]
            if side == Side.TARGET:
                if rf.forward is not None:
                    if value not in rf.forward:
                        raise UnmappedValue(rf.normalized, value)
                    value = rf.forward[value]
                if rf.target_format and rf.canonical_format:
                    value = convert_text(value, rf.canonical_format, rf.target_format)
            else:
                if rf.source_format and rf.canonical_format:
                    value = convert_text(value, rf.canonical_format, rf.source_format)
            out[self._native_name(rf, side)] = value
        return out

    def normalized_names(self) -> list[str]:
        return list(self._fields)
