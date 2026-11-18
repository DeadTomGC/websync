"""Tests for the config-driven Mapper."""

from __future__ import annotations

import textwrap

import pytest

from websync.mapping import (
    AmbiguousReverseType,
    Mapper,
    UnmappedType,
    UnmappedValue,
)
from websync.models import Side, SyncItem


def _validate_yaml(text: str):
    import yaml

    from websync.config import ProgramConfig

    return ProgramConfig.model_validate(yaml.safe_load(textwrap.dedent(text)))


@pytest.fixture
def link(tmp_path):
    cfg = _validate_yaml(
        """
        program: p
        links:
          - name: l1
            source: fake_a
            target: fake_b
            types:
              requirement: Requirement
              interface: Interface
              feature: Story          # deliberate fold: feature+story -> Story
              story: Story
            reverse_types:
              Story: story
            scope:
              source: {project: A}
              target: {project: B}
            fields:
              title: {source: name, target: name}
              body: {source: text, target: description, format: plain, source_format: html}
              status:
                source: status
                target: state
                value_map: {draft: open, approved: done}
              effort:
                source: effort
                target: estimate
                transform: man_days_to_points
              simple:
                source: s
                target: t
                value_map: {alpha: 1, beta: 1}   # ambiguous reverse
        """
    )
    return cfg.links[0]


def _validate_yaml(text: str):
    import textwrap as tw

    import yaml

    from websync.config import ProgramConfig

    return ProgramConfig.model_validate(yaml.safe_load(tw.dedent(text)))


def test_types_forward_and_fold(link):
    mapper = Mapper.for_link(link)
    assert mapper.target_type_for("requirement") == "Requirement"
    assert mapper.has_source_type("story") is True
    assert mapper.has_source_type("risk") is False
    assert mapper.source_type_for("Story") == "story"  # reverse_types resolves fold
    with pytest.raises(UnmappedType):
        mapper.source_type_for("Unknown")


def test_reverse_fold_without_reverse_types_is_ambiguous():
    cfg = _validate_yaml(
        """
        program: p
        links:
          - name: l1
            source: fake_a
            target: fake_b
            types: {feature: Story, story: Story}
            scope: {source: {project: A}, target: {project: B}}
        """
    )
    mapper = Mapper.for_link(cfg.links[0])
    with pytest.raises(AmbiguousReverseType):
        mapper.source_type_for("Story")


def test_to_normalized_source_applies_transform_and_format(link):
    mapper = Mapper.for_link(link)
    item = SyncItem(
        native_id="S1",
        type="requirement",
        fields={
            "name": "req",
            "text": "<p>body &amp; more</p>",
            "effort": "2.5",
            "status": "approved",
        },
    )
    norm = mapper.to_normalized(item, Side.SOURCE)
    assert norm["title"] == "req"
    assert norm["body"] == "body & more"  # html -> plain canonical
    assert norm["effort"] == 5  # man_days_to_points
    assert norm["status"] == "approved"


def test_source_value_outside_value_map_reaches_target_as_unmapped(link):
    mapper = Mapper.for_link(link)
    item = SyncItem(native_id="S2", type="requirement", fields={"status": "mystery"})
    # Source side passes the native value through (source vocab = map keys)...
    norm = mapper.to_normalized(item, Side.SOURCE)
    assert norm["status"] == "mystery"
    # ...but pushing it to the target has no value_map entry -> UnmappedValue.
    with pytest.raises(UnmappedValue):
        mapper.from_normalized({"status": "mystery"}, Side.TARGET)


def test_from_normalized_target_applies_value_map(link):
    mapper = Mapper.for_link(link)
    out = mapper.from_normalized({"status": "approved", "title": "x"}, Side.TARGET)
    assert out == {"state": "done", "name": "x"}


def test_to_normalized_target_reverse_value_map(link):
    mapper = Mapper.for_link(link)
    item = SyncItem(native_id="T1", type="Story", fields={"state": "done"})
    norm = mapper.to_normalized(item, Side.TARGET)
    assert norm["status"] == "approved"
    bad = SyncItem(native_id="T2", type="Story", fields={"state": "unknown"})
    with pytest.raises(UnmappedValue):
        mapper.to_normalized(bad, Side.TARGET)


def test_ambiguous_reverse_value_map_raises(link):
    mapper = Mapper.for_link(link)
    item = SyncItem(native_id="T3", type="Story", fields={"t": 1})
    with pytest.raises(UnmappedValue):
        mapper.to_normalized(item, Side.TARGET)


def test_target_to_source_roundtrip_via_value_map(link):
    mapper = Mapper.for_link(link)
    native = mapper.from_normalized({"status": "approved"}, Side.TARGET)
    assert native == {"state": "done"}
    back = mapper.to_normalized(SyncItem(native_id="T", type="Story", fields=native), Side.TARGET)
    assert back["status"] == "approved"


def test_format_conversion_on_from_normalized(link):
    mapper = Mapper.for_link(link)
    # canonical plain -> source html
    out = mapper.from_normalized({"body": "line1\nline2"}, Side.SOURCE)
    assert out["text"].startswith("<p>line1")
    assert "<br>" in out["text"]


def test_unknown_transform_rejected_at_mapper_build():
    cfg = _validate_yaml(
        """
        program: p
        links:
          - name: l1
            source: a
            target: b
            types: {requirement: Requirement}
            scope: {source: {project: A}, target: {project: B}}
            fields:
              x: {source: xs, target: xt, transform: nope}
        """
    )
    with pytest.raises(ValueError, match="unknown transform"):
        Mapper.for_link(cfg.links[0])


def test_unknown_format_rejected_at_mapper_build():
    cfg = _validate_yaml(
        """
        program: p
        links:
          - name: l1
            source: a
            target: b
            types: {requirement: Requirement}
            scope: {source: {project: A}, target: {project: B}}
            fields:
              x: {source: xs, target: xt, format: rtf}
        """
    )
    with pytest.raises(ValueError, match="format"):
        Mapper.for_link(cfg.links[0])
