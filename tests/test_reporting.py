"""Tests for Markdown report rendering."""

from __future__ import annotations

import textwrap

from websync.config import ProgramConfig
from websync.models import RunStats
from websync.reporting import render_report


def _config():
    import yaml

    raw = yaml.safe_load(
        textwrap.dedent(
            """
            program: prog
            links:
              - name: link-a-b
                source: toolA
                target: toolB
                types: {requirement: Requirement}
                scope: {source: {project: A}, target: {project: B}}
                fields: {title: {source: name, target: name}}
                direction: bidirectional
                conflict_strategy: skip_and_report
            """
        )
    )
    return ProgramConfig.model_validate(raw)


def test_report_contains_run_summary_and_events():
    cfg = _config()
    link = cfg.links[0]
    stats = RunStats(created=2, updated=1, conflicts=3, skipped=1, errors=0)
    events = [
        {
            "action": "create",
            "side": "source",
            "native_id": "A1",
            "counterpart": "B1",
            "detail": "created target item",
        },
        {"action": "update", "side": "target", "native_id": "B1"},
        {
            "action": "conflict",
            "native_id": "A2",
            "counterpart": "B2",
            "reason": "changed_on_both_sides",
            "detail": "strategy=skip_and_report",
        },
        {"action": "skip", "native_id": "A3", "detail": "type not mapped"},
        {"action": "error", "native_id": "A4", "detail": "boom"},
    ]
    md = render_report("prog", link, 42, dry_run=True, stats=stats, events=events)
    assert "prog / link-a-b" in md
    assert "*(dry run)*" in md
    assert "| 2 | 1 | 3 | 1 | 0 |" in md
    assert "CREATED" in md and "`A1`" in md
    assert "UPDATED" in md
    assert "CONFLICT" in md and "changed_on_both_sides" in md
    assert "skipped" in md
    assert "ERROR" in md and "boom" in md


def test_report_empty_events():
    cfg = _config()
    md = render_report(
        "prog",
        cfg.links[0],
        1,
        dry_run=False,
        stats=RunStats(),
        events=[],
    )
    assert "(no changes)" in md
    assert "dry run" not in md
