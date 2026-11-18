"""Tests for the CLI surface."""

from __future__ import annotations

from typer.testing import CliRunner

from websync.cli import app

runner = CliRunner()

VALID_CONFIG = """
program: p1
links:
  - name: l1
    source: cameo_twc
    target: jama
    types: {requirement: requirement}
    scope:
      source: {project: X}
      target: {project: 1}
    fields:
      title: {source: name, target: name}
"""


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "websync" in result.output


def test_validate_config_ok(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(VALID_CONFIG)
    result = runner.invoke(app, ["validate-config", str(cfg)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_validate_config_failure_exits_nonzero(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("program: p1\nlinks: []\n")
    result = runner.invoke(app, ["validate-config", str(cfg)])
    assert result.exit_code == 1


def test_validate_config_rejects_unknown_transform(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        VALID_CONFIG.replace(
            "title: {source: name, target: name}",
            "title: {source: name, target: name, transform: nope}",
        )
    )
    result = runner.invoke(app, ["validate-config", str(cfg)])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_run_with_fake_adapters_end_to_end(tmp_path):
    """Full pipeline smoke: fake adapters, real engine/state/report/report."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(VALID_CONFIG.replace("source: cameo_twc", "source: fake"))
    db = tmp_path / "state.db"
    reports = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(cfg),
            "--fake",
            "--db",
            str(db),
            "--reports-dir",
            str(reports),
        ],
    )
    assert result.exit_code == 0
    assert "1 link run(s)" in result.output
    assert (reports).exists()
    assert db.exists()


def test_run_without_fake_adapters_not_implemented(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(VALID_CONFIG)
    result = runner.invoke(app, ["run", "--config", str(cfg), "--db", str(tmp_path / "s.db")])
    assert result.exit_code == 2


def test_run_rejects_invalid_config(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("program: p1\nlinks: []\n")
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code == 1


def test_status_and_report_on_empty_db(tmp_path):
    result = runner.invoke(app, ["status", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code == 1
    result = runner.invoke(app, ["report", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code == 1


def test_status_and_report_after_a_fake_run(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(VALID_CONFIG.replace("source: cameo_twc", "source: fake"))
    db = tmp_path / "state.db"
    run = runner.invoke(app, ["run", "--config", str(cfg), "--fake", "--db", str(db)])
    assert run.exit_code == 0
    status = runner.invoke(app, ["status", "--db", str(db)])
    assert status.exit_code == 0
    assert "l1" in status.output
    report = runner.invoke(app, ["report", "--db", str(db)])
    assert report.exit_code == 0
    assert "p1 / l1" in report.output
