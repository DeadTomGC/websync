"""Smoke tests for the CLI surface."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from websync.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "websync" in result.output


def test_validate_config_ok(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "program: p1\n"
        "links:\n"
        "  - name: l1\n"
        "    source: cameo_twc\n"
        "    target: jama\n"
        "    types: {requirement: requirement}\n"
        "    scope:\n"
        "      source: {project: X}\n"
        "      target: {project: 1}\n"
    )
    result = runner.invoke(app, ["validate-config", str(cfg)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_validate_config_failure_exits_nonzero(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("program: p1\nlinks: []\n")
    result = runner.invoke(app, ["validate-config", str(cfg)])
    assert result.exit_code == 1


@pytest.mark.parametrize("cmd", [["run", "--config", "x.yaml"], ["status"], ["report"]])
def test_placeholder_commands_succeed(cmd):
    result = runner.invoke(app, cmd)
    assert result.exit_code == 0
