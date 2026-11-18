"""Contract tests for the Cameo Teamwork Cloud adapter (HTTP mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import responses

from websync.adapters.base import SyncError
from websync.adapters.cameo_twc import CameoTWCAdapter
from websync.adapters.http import TransientHTTPError
from websync.config import ScopeConfig

BASE = "https://twc.example:8443/twcloud/rest/v1"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CAMEO_TWC_BASE_URL", BASE)
    monkeypatch.setenv("CAMEO_TWC_USER", "user")
    monkeypatch.setenv("CAMEO_TWC_PASSWORD", "pass")


def _scope(**kw):
    return ScopeConfig(project=kw.pop("project", "Program A"), **kw)


def _element(id_, name, modified, type_="requirement", **props):
    return {"id": id_, "name": name, "type": type_, "modified": modified, "properties": props}


@responses.activate
def test_healthcheck_authenticates_and_resolves_project_by_name(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(f"{BASE}/projects", json={"projects": [{"id": 7, "name": "Program A"}]})

    adapter = CameoTWCAdapter.from_env(_scope())
    adapter.healthcheck()

    auth = responses.calls[0].request
    assert "Authorization" in auth.headers  # Basic auth on the login call
    assert adapter._token == "tok-1"


@responses.activate
def test_healthcheck_project_by_numeric_id_still_authenticates(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)

    adapter = CameoTWCAdapter.from_env(_scope(project=7))
    adapter.healthcheck()  # no /projects lookup needed for numeric IDs
    assert len(responses.calls) == 1


@responses.activate
def test_healthcheck_unknown_project_raises(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(f"{BASE}/projects", json={"projects": [{"id": 1, "name": "Other"}]})

    adapter = CameoTWCAdapter.from_env(_scope())
    with pytest.raises(SyncError, match="not found"):
        adapter.healthcheck()


@responses.activate
def test_fetch_changed_paginates_and_filters_by_date(env):
    old_date = (NOW - timedelta(days=30)).isoformat()
    new_date = NOW.isoformat()
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    # numeric project scope: no /projects lookup needed
    responses.get(
        f"{BASE}/projects/7/elements",
        json={"elements": [_element(101, "old", old_date)], "page": {"total": 2, "size": 1}},
        status=200,
    )
    responses.get(
        f"{BASE}/projects/7/elements",
        json={
            "elements": [_element(102, "new", new_date, criticality="high")],
            "page": {"total": 2, "size": 1},
        },
        status=200,
    )

    adapter = CameoTWCAdapter.from_env(_scope(project=7))
    items = adapter.fetch_changed(NOW - timedelta(hours=1))
    assert [i.native_id for i in items] == ["102"]
    item = items[0]
    assert item.fields == {"name": "new", "criticality": "high"}
    assert item.type == "requirement"
    assert item.changed_at == NOW
    # pagination: two GETs, second with page=2
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 2
    assert "page=2" in gets[1].request.url


@responses.activate
def test_fetch_changed_item_type_filter(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(
        f"{BASE}/projects/7/elements",
        json={
            "elements": [
                _element(1, "req", NOW.isoformat(), type_="requirement"),
                _element(2, "other", NOW.isoformat(), type_="package"),
            ]
        },
        status=200,
    )
    adapter = CameoTWCAdapter.from_env(_scope(project=7, item_type_filter=["requirement"]))
    assert [i.native_id for i in adapter.fetch_changed(NOW)] == ["1"]


@responses.activate
def test_get_and_update(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(
        f"{BASE}/elements/101",
        json=_element(101, "req", NOW.isoformat(), status="draft"),
        status=200,
    )
    responses.patch(f"{BASE}/elements/101", json={}, status=200)

    adapter = CameoTWCAdapter.from_env(_scope(project=7))
    item = adapter.get("101")
    assert item.fields == {"name": "req", "status": "draft"}

    adapter.update("101", {"name": "renamed", "status": "approved"})
    patch = responses.calls[-1].request
    assert json.loads(patch.body) == {"name": "renamed", "properties": {"status": "approved"}}


@responses.activate
def test_create_returns_native_id(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.post(f"{BASE}/projects/7/elements", json={"id": 555}, status=200)

    adapter = CameoTWCAdapter.from_env(_scope(project=7))
    from websync.models import SyncItem

    new_id = adapter.create(
        SyncItem(native_id="", type="requirement", fields={"name": "new", "specification": "text"})
    )
    assert new_id == "555"
    body = json.loads(responses.calls[-1].request.body)
    assert body == {"name": "new", "properties": {"specification": "text"}, "type": "requirement"}


@responses.activate
def test_401_triggers_reauth_and_retry(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(f"{BASE}/projects", json={"projects": []}, status=401)  # first attempt
    responses.get(
        f"{BASE}/projects", json={"projects": [{"id": 7, "name": "Program A"}]}, status=200
    )

    adapter = CameoTWCAdapter.from_env(_scope())
    adapter._resolve_project()  # 401 -> re-auth -> retry succeeds
    auth_calls = [c for c in responses.calls if c.request.url.endswith("/authentication")]
    assert len(auth_calls) == 2


@responses.activate
def test_5xx_retried_then_succeeds(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(f"{BASE}/projects", status=500)
    responses.get(
        f"{BASE}/projects", json={"projects": [{"id": 7, "name": "Program A"}]}, status=200
    )

    adapter = CameoTWCAdapter.from_env(_scope())
    adapter._resolve_project()
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 2


@responses.activate
def test_persistent_5xx_raises_transient(env):
    responses.post(f"{BASE}/authentication", json={"token": "tok-1"}, status=200)
    responses.get(f"{BASE}/projects", status=500)
    responses.get(f"{BASE}/projects", status=500)
    responses.get(f"{BASE}/projects", status=500)

    adapter = CameoTWCAdapter.from_env(_scope())
    with pytest.raises(TransientHTTPError):
        adapter._resolve_project()


@responses.activate
def test_error_response_raises_sync_error(env):
    responses.post(f"{BASE}/authentication", json={"error": "bad credentials"}, status=401)
    adapter = CameoTWCAdapter.from_env(_scope())
    with pytest.raises(SyncError, match="HTTP 401"):
        adapter.healthcheck()
