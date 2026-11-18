"""Tests for config loading and validation."""

from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from websync.config import ProgramConfig, load_config


def _valid_yaml() -> str:
    return textwrap.dedent(
        """
        program: programA
        links:
          - name: cameo-jama-requirements
            source: cameo_twc
            target: jama
            types:
              requirement: requirement
            scope:
              source: {project: "Program A / SysModel"}
              target: {project: 123}
            fields:
              title:
                source: name
                target: name
              status:
                source: status
                target: status
                value_map:
                  draft: "In Progress"
                  approved: "Approved"
            direction: bidirectional
            conflict_strategy: newest_wins
            delete_behavior: skip
        """
    )


def test_valid_config_loads(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(_valid_yaml())
    cfg = load_config(f)
    assert cfg.program == "programA"
    assert cfg.links[0].source == "cameo_twc"
    assert cfg.links[0].fields["status"].value_map == {
        "draft": "In Progress",
        "approved": "Approved",
    }


def test_link_lookup_by_name(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(_valid_yaml())
    cfg = load_config(f)
    assert cfg.link("cameo-jama-requirements").target == "jama"
    with pytest.raises(KeyError):
        cfg.link("does-not-exist")


@pytest.mark.parametrize(
    "bad_value,field,original",
    [
        ("sideways", "direction", "bidirectional"),
        ("overwrite_everything", "conflict_strategy", "newest_wins"),
        ("hard_delete", "delete_behavior", "skip"),
    ],
)
def test_invalid_enums_rejected(tmp_path, bad_value, field, original):
    yaml_text = _valid_yaml().replace(f"{field}: {original}", f"{field}: {bad_value}")
    f = tmp_path / "cfg.yaml"
    f.write_text(yaml_text)
    with pytest.raises(ValidationError):
        load_config(f)


def test_unknown_top_level_key_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(_valid_yaml() + "\nmystery_key: true\n")
    with pytest.raises(ValidationError):
        load_config(f)


def test_empty_config_rejected():
    with pytest.raises(ValidationError):
        ProgramConfig.model_validate({})
