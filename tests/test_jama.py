"""Contract tests for the Jama adapter (HTTP mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import responses

from websync.adapters.base import SyncError
from websync.adapters.jama import JamaAdapter
from websync.config import ScopeConfig
from websync.models import SyncItem

BASE = "https://jama.example.com"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("JAMA_BASE_URL", BASE)
    monkeypatch.setenv("JAMA_CLIENT_ID", "cid")
    monkeypatch.setenv("JAMA_CLIENT_SECRET", "secret")


def _scope(**kw):
    return ScopeConfig(project=kw.pop("project", 123), **kw)


def _item(id_, name, modified, item_type=128, description="", field_data=None):
    location = {}
    if field_data:
        location["fieldData"] = field_data
    return {
        "id": id_,
        "name": name,
        "description": description,
        "itemType": item_type,
        "lastActivityDate": modified,
        "location": location,
    }


def _auth_ok():
    responses.post(f"{BASE}/rest/oauth/token", json={"access_token": "tok-1"}, status=200)


@responses.activate
def test_oauth_and_healthcheck(env):
    _auth_ok()
    responses.get(f"{BASE}/rest/v1/users/current", json={"data": {"id": 9}}, status=200)

    adapter = JamaAdapter.from_env(_scope())
    adapter.healthcheck()

    token_req = responses.calls[0].request
    assert token_req.url.endswith("/rest/oauth/token")
    assert "grant_type=client_credentials" in token_req.body


@responses.activate
def test_non_numeric_project_rejected(env):
    _auth_ok()
    adapter = JamaAdapter.from_env(_scope(project="programA"))
    with pytest.raises(SyncError, match="numeric"):
        adapter._project_id()


@responses.activate
def test_fetch_changed_paginates_filters_and_flattens(env):
    _auth_ok()
    old = (NOW - timedelta(days=3)).isoformat()
    responses.get(
        f"{BASE}/rest/v1/items",
        json={
            "data": [_item(11, "old req", old, field_data={"412": {"100": "In Progress"}})],
            "meta": {"pageInfo": {"startIndex": 0, "resultCount": 1, "totalResults": 2}},
        },
        status=200,
    )
    responses.get(
        f"{BASE}/rest/v1/items",
        json={
            "data": [_item(12, "new req", NOW.isoformat(), item_type=129)],
            "meta": {"pageInfo": {"startIndex": 1, "resultCount": 1, "totalResults": 2}},
        },
        status=200,
    )
    responses.get(f"{BASE}/rest/v1/itemtypes/129", json={"data": {"key": "feature"}}, status=200)

    adapter = JamaAdapter.from_env(_scope())
    items = adapter.fetch_changed(NOW - timedelta(hours=1))
    assert [i.native_id for i in items] == ["12"]  # old item filtered by date
    item = items[0]
    assert item.type == "feature"  # itemTypeId 129 resolved via key
    assert item.fields["name"] == "new req"

    # custom field flattening is verified directly on _to_item (type 129 is
    # already resolved/cached by the fetch above):
    flattened = adapter._to_item(
        _item(11, "x", NOW.isoformat(), item_type=129, field_data={"412": {"100": "v"}})
    )
    assert flattened.fields["fieldData.412.100"] == "v"


@responses.activate
def test_fetch_changed_item_type_filter(env):
    _auth_ok()
    responses.get(
        f"{BASE}/rest/v1/items",
        json={
            "data": [_item(12, "req", NOW.isoformat())],
            "meta": {"pageInfo": {"startIndex": 0, "resultCount": 1, "totalResults": 1}},
        },
        status=200,
    )
    responses.get(
        f"{BASE}/rest/v1/itemtypes/128", json={"data": {"key": "requirement"}}, status=200
    )

    adapter = JamaAdapter.from_env(_scope(item_type_filter=["risk"]))
    assert adapter.fetch_changed(NOW) == []


@responses.activate
def test_get(env):
    _auth_ok()
    responses.get(
        f"{BASE}/rest/v1/items/77",
        json={"data": _item(77, "req", NOW.isoformat(), description="d")},
        status=200,
    )
    responses.get(
        f"{BASE}/rest/v1/itemtypes/128", json={"data": {"key": "requirement"}}, status=200
    )

    adapter = JamaAdapter.from_env(_scope())
    item = adapter.get("77")
    assert item.fields == {"name": "req", "description": "d"}
    assert item.type == "requirement"


@responses.activate
def test_create_resolves_type_and_sets_custom_fields(env):
    _auth_ok()
    responses.get(
        f"{BASE}/rest/v1/itemtypes",
        json={"data": [{"id": 128, "key": "requirement", "label": "Requirement"}]},
        status=200,
    )
    responses.post(
        f"{BASE}/rest/v1/items",
        json={"data": {"id": 551}},
        status=200,
    )
    responses.patch(f"{BASE}/rest/v1/items/551", json={}, status=200)

    adapter = JamaAdapter.from_env(_scope())
    new_id = adapter.create(
        SyncItem(
            native_id="",
            type="requirement",
            fields={
                "name": "new req",
                "description": "body",
                "fieldData.412.100": "Approved",
            },
        )
    )
    assert new_id == "551"
    create_body = json.loads(
        [c for c in responses.calls if c.request.method == "POST"][-1].request.body
    )
    assert create_body == {
        "project": 123,
        "itemTypeId": 128,
        "name": "new req",
        "description": "body",
    }
    patch_body = json.loads(responses.calls[-1].request.body)
    assert patch_body == {"location": {"fieldData": {"412": {"100": "Approved"}}}}


@responses.activate
def test_update_merges_core_and_custom_fields(env):
    _auth_ok()
    responses.patch(f"{BASE}/rest/v1/items/77", json={}, status=200)

    adapter = JamaAdapter.from_env(_scope())
    adapter.update("77", {"name": "renamed", "fieldData.412.100": "Done"})
    patch_body = json.loads(responses.calls[-1].request.body)
    assert patch_body == {
        "name": "renamed",
        "location": {"fieldData": {"412": {"100": "Done"}}},
    }


@responses.activate
def test_401_triggers_reauth_and_retry(env):
    _auth_ok()  # first token
    responses.get(f"{BASE}/rest/v1/users/current", json={}, status=401)
    responses.get(f"{BASE}/rest/v1/users/current", json={"data": {"id": 9}}, status=200)

    adapter = JamaAdapter.from_env(_scope())
    adapter.healthcheck()
    token_calls = [c for c in responses.calls if c.request.url.endswith("/oauth/token")]
    assert len(token_calls) == 2


@responses.activate
def test_unknown_item_type_raises(env):
    _auth_ok()
    responses.get(
        f"{BASE}/rest/v1/itemtypes", json={"data": [{"id": 128, "key": "requirement"}]}, status=200
    )

    adapter = JamaAdapter.from_env(_scope())
    with pytest.raises(SyncError, match="unknown item type"):
        adapter.create(SyncItem(native_id="", type="nope", fields={"name": "x"}))


@responses.activate
def test_missing_env_vars_rejected(monkeypatch):
    monkeypatch.delenv("JAMA_BASE_URL", raising=False)
    with pytest.raises(SyncError, match="JAMA_BASE_URL"):
        JamaAdapter.from_env(_scope())
