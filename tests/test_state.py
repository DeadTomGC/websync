"""Tests for the SQLite state store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from websync.state import Pair, StateError, StateStore, field_hash


def _store(tmp_path):
    return StateStore(tmp_path / "state.db")


def test_field_hash_stable_regardless_of_key_order():
    assert field_hash({"a": 1, "b": "x"}) == field_hash({"b": "x", "a": 1})


def test_run_lifecycle(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run("prog", "cfg.yaml", "link1", dry_run=False)
    assert store.get_run(run_id)["status"] == "running"
    store.finish_run(run_id, "success", {"created": 2})
    row = store.get_run(run_id)
    assert row["status"] == "success"
    assert row["stats_json"]
    # last successful run is now known
    assert store.last_successful_started("link1") is not None
    # dry runs never count as last successful
    dry_id = store.start_run("prog", "cfg.yaml", "link1", dry_run=True)
    store.finish_run(dry_id, "success", {})
    assert store.last_successful_started("link1") > datetime.now(UTC) - timedelta(seconds=5)


def test_start_run_refuses_active_run(tmp_path):
    store = _store(tmp_path)
    store.start_run("prog", "cfg.yaml", "link1", dry_run=False)
    with pytest.raises(StateError):
        store.start_run("prog", "cfg.yaml", "link1", dry_run=False)


def test_stale_running_run_expires(tmp_path):
    store = _store(tmp_path)
    stale_id = store.start_run("prog", "cfg.yaml", "link1", dry_run=False)
    # backdate the run to simulate a crashed CI job
    old = datetime.now(UTC) - timedelta(hours=25)
    store._conn.execute(
        "UPDATE sync_runs SET started = ? WHERE id = ?", (old.isoformat(), stale_id)
    )
    store._conn.commit()
    new_id = store.start_run("prog", "cfg.yaml", "link1", dry_run=False)
    assert store.get_run(new_id)["status"] == "running"
    assert store.get_run(stale_id)["status"] == "failed"


def test_put_and_get_pair(tmp_path):
    store = _store(tmp_path)
    store.put_pair("link1", Pair("S1", "T1", "h-s", "h-t"))
    pair = store.get_pair("link1", "source", "S1")
    assert pair == Pair("S1", "T1", "h-s", "h-t")
    # lookup from either side
    assert store.get_pair("link1", "target", "T1") == pair
    assert store.get_pair("link1", "source", "missing") is None


def test_conflicts_and_reports_roundtrip(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run("prog", "cfg.yaml", "link1", dry_run=False)
    store.record_conflict(run_id, "link1", "X1", {"reason": "changed_on_both_sides"})
    conflicts = store.conflicts_for_run(run_id)
    assert len(conflicts) == 1
    assert conflicts[0]["native_id"] == "X1"
    store.save_report(run_id, "# report")
    assert store.get_report(run_id) == "# report"
    assert store.get_report(424242) is None
