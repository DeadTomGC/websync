"""Cameo Teamwork Cloud adapter (Phase 2).

No maintained upstream Python client exists for TWC's REST API, so this will
be a minimal `requests`-based client scoped to the endpoints we use.
See docs/tools/cameo-twc.md and the plan's library-integrations doc.
"""

from __future__ import annotations


class CameoTWCAdapter:
    """Stub — implemented in Phase 2."""

    tool_name = "cameo_twc"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError("CameoTWCAdapter arrives in Phase 2")
