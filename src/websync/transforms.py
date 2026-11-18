"""Named value transforms referenced from mapping YAML.

Configs reference these by name (``transform: man_days_to_points``) so field
arithmetic stays code but mappings stay declarative. Keep the registry small;
document every addition in docs/plan/03-mapping-configuration-spec.md.

Phase 1 limitation: transforms apply on the SOURCE->normalized direction
only (no inverse resolution), so avoid them on bidirectional fields until
inverses are supported.
"""

from __future__ import annotations

from collections.abc import Callable

TransformFn = Callable[[object], object]

_REGISTRY: dict[str, TransformFn] = {}


def register(name: str) -> Callable[[TransformFn], TransformFn]:
    def deco(fn: TransformFn) -> TransformFn:
        if name in _REGISTRY:
            raise ValueError(f"transform {name!r} already registered")
        _REGISTRY[name] = fn
        return fn

    return deco


def apply_transform(name: str, value):
    if name not in _REGISTRY:
        raise KeyError(f"unknown transform {name!r} (known: {sorted(_REGISTRY)})")
    return _REGISTRY[name](value)


def known_transforms() -> list[str]:
    return sorted(_REGISTRY)


@register("man_days_to_points")
def man_days_to_points(value):
    """Convert effort in man-days to story points (1 man-day == 2 points)."""
    if value in (None, ""):
        return value
    return round(float(value) * 2)


@register("uppercase")
def uppercase(value):
    """Uppercase a string value (handy for ID-like fields)."""
    return value.upper() if isinstance(value, str) else value
