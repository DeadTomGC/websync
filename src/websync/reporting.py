"""Markdown report rendering for sync runs (emitted as CI artifacts)."""

# ruff: noqa: E501 (Jinja template lines are allowed to exceed 100 chars)

from __future__ import annotations

from jinja2 import Environment, StrictUndefined, select_autoescape

from websync.models import RunStats

_TEMPLATE = """\
# Sync report — {{ program }} / {{ link.name }}

- **Run:** {{ run_id }}
{%- if dry_run %} *(dry run)*
{%- endif %}
- **Direction:** {{ link.direction }}
- **Conflict strategy:** {{ link.conflict_strategy }}
- **Tools:** {{ link.source }} &rarr; {{ link.target }}

| created | updated | conflicts | skipped | errors |
|--------:|--------:|----------:|--------:|-------:|
| {{ stats.created }} | {{ stats.updated }} | {{ stats.conflicts }} | {{ stats.skipped }} | {{ stats.errors }} |

## Changes

{% for e in events %}
{%- if e.action == "create" %}
- **CREATED** {{ e.get("side") }} `{{ e.native_id }}`{% if e.get("counterpart") %} &rarr; counterpart `{{ e.get("counterpart") }}`{% endif %} — {{ e.get("detail", "") }}
{%- elif e.action == "update" %}
- **UPDATED** {{ e.get("side") }} `{{ e.native_id }}`{% if e.get("detail") %} — {{ e.get("detail") }}{% endif %}
{%- elif e.action == "conflict" %}
- **CONFLICT** `{{ e.native_id }}`{% if e.get("counterpart") %} / `{{ e.get("counterpart") }}`{% endif %} — {{ e.get("reason") }}: {{ e.get("detail", "") }}
{%- elif e.action == "skip" %}
- skipped `{{ e.native_id }}` — {{ e.get("detail", "") }}
{%- elif e.action == "error" %}
- **ERROR** `{{ e.native_id }}` — {{ e.get("detail", "") }}
{%- endif %}
{%- endfor %}
{% if not events %}
*(no changes)*
{% endif %}
"""

_env = Environment(
    autoescape=select_autoescape([]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


def render_report(
    program: str,
    link,
    run_id: int,
    dry_run: bool,
    stats: RunStats,
    events: list[dict],
) -> str:
    """Render one link-run's Markdown report."""
    return _env.from_string(_TEMPLATE).render(
        program=program,
        link=link,
        run_id=run_id,
        dry_run=dry_run,
        stats=stats,
        events=events,
    )
