"""Jira adapter (Phase 3).

Wraps the maintained `jira` PyPI library (extra: `pip install websync[jira]`).
The adapter is mostly glue: auth via PAT, JQL for change queries, CRUD for
epics/stories. See docs/tools/jira.md and the plan's library-integrations doc.
"""

from __future__ import annotations


class JiraAdapter:
    """Stub — implemented in Phase 3."""

    tool_name = "jira"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError("JiraAdapter arrives in Phase 3")

    @classmethod
    def from_env(cls, scope=None) -> JiraAdapter:
        raise NotImplementedError("JiraAdapter arrives in Phase 3")
