"""Hardening: no duplicate-creating retries, no remote bodies in errors, partial runs."""

from __future__ import annotations

import pytest
import responses

from websync.adapters.base import SyncError
from websync.adapters.http import JSONClient, TransientHTTPError
from websync.adapters.jama import _nest_custom_fields
from websync.state import StateError, StateStore

BASE = "https://t.example"


def _client():
    return JSONClient("t", BASE, retry_multiplier=0)


@responses.activate
def test_post_not_retried_on_5xx():
    responses.post(f"{BASE}/x", status=500, body="secret-internal-detail")
    responses.post(f"{BASE}/x", json={"id": 1})
    with pytest.raises(TransientHTTPError):
        _client().post("/x", json_body={})
    assert len(responses.calls) == 1


@responses.activate
def test_post_retried_on_429():
    responses.post(f"{BASE}/x", status=429)
    responses.post(f"{BASE}/x", json={"id": 1})
    assert _client().post("/x", json_body={}) == {"id": 1}


@responses.activate
def test_get_still_retried_on_5xx():
    responses.get(f"{BASE}/x", status=503)
    responses.get(f"{BASE}/x", json={"ok": True})
    assert _client().get("/x") == {"ok": True}


@responses.activate
def test_error_messages_exclude_remote_body():
    responses.get(f"{BASE}/x", status=403, body="token=SECRET123")
    with pytest.raises(SyncError) as ei:
        _client().get("/x")
    assert "SECRET123" not in str(ei.value)
    responses.get(f"{BASE}/y", status=500, body="token=SECRET123")
    with pytest.raises(SyncError) as ei:
        _client().get("/y")
    assert "SECRET123" not in str(ei.value)


@responses.activate
def test_non_json_response_is_sync_error():
    responses.get(f"{BASE}/x", status=200, body="<html>not json</html>")
    with pytest.raises(SyncError, match="not valid JSON"):
        _client().get("/x")


def test_bad_custom_field_path_is_sync_error():
    with pytest.raises(SyncError):
        _nest_custom_fields({"fieldData.412": 1})


def test_run_guard_unique_index(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.start_run("p", "c", "l", False)
    with pytest.raises(StateError):
        store.start_run("p", "c", "l", False)
    # raw insert bypassing the check is also rejected by the DB
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO sync_runs (program, config_path, link_name, started, status)"
            " VALUES ('p','c','l','now','running')"
        )
