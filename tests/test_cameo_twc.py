"""Contract tests for the Cameo Teamwork Cloud adapter (atwc over stub HTTP).

Fixture payloads mirror atwc's TWC REST shapes:
- login/logout are bare ``204`` responses (Basic auth on the session),
- resources are listed via ``GET /resources?includeBody=true``,
- elements are read one-by-one (``GET /resources/{rid}/elements/{id}``,
  payload is a list whose [1] is the element) or in batches
  (``POST /resources/{rid}/elements`` with a JSON list of element IDs,
  payload maps ID -> ``{"data": [meta, element]}``),
- element data lives in ``kerml:esiData``; tagged values hang off the
  element via ``@id`` references and carry a ``tagDefinition`` reference.

Shapes flagged VERIFY-live in docs/tools/cameo-twc.md are the ones these
fixtures assert against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from websync.adapters.base import AuthError, SyncError
from websync.adapters.cameo_twc import CameoTWCAdapter, TransientTWCError
from websync.config import ScopeConfig

NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
RESOURCES_PATH = "/resources?includeBody=true"
RID = 7


@pytest.fixture
def env(monkeypatch, twc_stub):
    monkeypatch.setenv("CAMEO_TWC_BASE_URL", twc_stub.url)
    monkeypatch.setenv("CAMEO_TWC_USER", "user")
    monkeypatch.setenv("CAMEO_TWC_PASSWORD", "pass")
    return twc_stub


def _scope(**kw):
    return ScopeConfig(project=kw.pop("project", "Program A"), **kw)


def _resources():
    return [{"ID": RID, "dcterms:title": "Program A.MASTER", "categoryID": 1}]


def _add_login_logout(stub, *, login_status=204):
    stub.add("GET", "/login", status=login_status, repeat=20)
    stub.add("GET", "/logout", status=204, repeat=20)


def _add_project(stub):
    stub.add("GET", RESOURCES_PATH, json=_resources(), repeat=20)


def _element(eid, name, *, type_="requirement", modified=None, tagged=None, **refs):
    esi = {"name": name}
    if modified:
        esi["modified"] = modified
    for value in (tagged or {}).values():
        esi["taggedValue"] = esi.get("taggedValue", []) + [{"@id": tv} for tv in value]
    for key, ids in refs.items():
        esi[key] = [{"@id": i} for i in ids]
    element = {"kerml:esiData": esi}
    if type_:
        element["@type"] = type_
    return element


def _batch_response(elements):
    """atwc batch shape: element ID -> {"data": [meta, element]}."""
    return {eid: {"data": [{"meta": True}, element]} for eid, element in elements.items()}


def _tagged_value(value, def_id):
    return {"kerml:esiData": {"value": value, "tagDefinition": [{"@id": def_id}]}}


def _add_full_model(stub, elements, roots, tagged_values, tagdefs):
    """Register the route set a full fetch traversal needs."""
    stub.add("GET", f"/resources/{RID}/revisions", json=["rev-1"], repeat=5)
    stub.add("GET", f"/resources/{RID}/revisions/rev-1", json=[{"rootObjectIDs": roots}], repeat=5)
    # one batch POST per traversal wave, matched by requested element IDs
    stub.add(
        "POST",
        f"/resources/{RID}/elements",
        body=roots,
        json=_batch_response(
            {
                r: _element(
                    r,
                    "model",
                    type_=None,
                    **{"ownedElements": [e for e in elements if e not in roots]},
                )
                for r in roots
            }
        ),
    )
    stub.add(
        "POST", f"/resources/{RID}/elements", body=list(elements), json=_batch_response(elements)
    )
    # also register single-element waves (batch_size=1 forces these)
    for eid, element in elements.items():
        stub.add(
            "POST", f"/resources/{RID}/elements", body=[eid], json=_batch_response({eid: element})
        )
    for tv_id, (value, def_id) in tagged_values.items():
        # repeat=2: tagged values are fetched once by the traversal and once
        # when mapping the owning element's fields
        stub.add(
            "POST",
            f"/resources/{RID}/elements",
            body=[tv_id],
            json=_batch_response({tv_id: _tagged_value(value, def_id)}),
            repeat=2,
        )
    for def_id, name in tagdefs.items():
        stub.add(
            "GET",
            f"/resources/{RID}/elements/{def_id}",
            json=[{"meta": True}, {"kerml:esiData": {"name": name}}],
        )
        # tag definitions are traversed like any other referenced element
        stub.add(
            "POST",
            f"/resources/{RID}/elements",
            body=[def_id],
            json=_batch_response({def_id: {"kerml:esiData": {"name": name}}}),
        )


def _no_retry(adapter):
    adapter.settings.retry_multiplier = 0.0
    return adapter


# ----------------------------------------------------------------- healthcheck


def test_healthcheck_resolves_project_by_title(env):
    _add_login_logout(env)
    _add_project(env)

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    adapter.healthcheck()

    login = env.calls_for("GET", "/login")[0]
    assert login["headers"]["Authorization"].startswith("Basic ")
    assert adapter._resource["ID"] == RID


def test_healthcheck_project_by_resource_id(env):
    _add_login_logout(env)
    _add_project(env)

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope(project=RID)))
    adapter.healthcheck()
    assert adapter._resource["ID"] == RID


def test_healthcheck_unknown_project_raises(env):
    _add_login_logout(env)
    _add_project(env)

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope(project="Nope")))
    with pytest.raises(SyncError, match="not found"):
        adapter.healthcheck()


def test_bad_credentials_raise_auth_error(env):
    _add_login_logout(env, login_status=401)

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    with pytest.raises(AuthError, match="login rejected"):
        adapter.healthcheck()


# ---------------------------------------------------------------- fetch/get


def test_fetch_changed_traverses_filters_and_maps_tagged_values(env, monkeypatch):
    old_date = (NOW - timedelta(days=30)).isoformat()
    new_date = NOW.isoformat()
    elements = {
        "req-101": _element("req-101", "old", modified=old_date),
        "req-102": _element(
            "req-102", "new", modified=new_date, tagged={"criticality": ["tv-crit"]}
        ),
    }
    _add_login_logout(env)
    _add_project(env)
    _add_full_model(
        env,
        elements,
        ["root-1"],
        tagged_values={"tv-crit": ("high", "def-crit")},
        tagdefs={"def-crit": "criticality"},
    )
    monkeypatch.setattr(CameoTWCAdapter, "batch_size", 1)  # force batched traversal

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    items = adapter.fetch_changed(NOW - timedelta(hours=1))

    assert [i.native_id for i in items] == ["req-102"]
    item = items[0]
    assert item.fields == {"name": "new", "criticality": "high"}
    assert item.type == "requirement"
    assert item.changed_at == NOW
    # batching: each element fetched in its own POST wave
    batches = [c["body"] for c in env.calls_for("POST", f"/resources/{RID}/elements")]
    assert batches[0] == ["root-1"]
    assert ["req-102"] in batches


def test_fetch_changed_item_type_filter(env):
    elements = {
        "req-1": _element("req-1", "req"),
        "pkg-1": _element("pkg-1", "other", type_="Package"),
    }
    _add_login_logout(env)
    _add_project(env)
    _add_full_model(env, elements, ["root-1"], tagged_values={}, tagdefs={})

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope(item_type_filter=["requirement"])))
    assert [i.native_id for i in adapter.fetch_changed(NOW)] == ["req-1"]


def test_get_returns_element_with_tagged_values(env):
    _add_login_logout(env)
    _add_project(env)
    env.add(
        "GET",
        f"/resources/{RID}/elements/101",
        json=[{"meta": True}, _element("101", "req", tagged={"status": ["tv-st"]})],
    )
    env.add(
        "POST",
        f"/resources/{RID}/elements",
        body=["tv-st"],
        json=_batch_response({"tv-st": _tagged_value("draft", "def-st")}),
    )
    env.add(
        "GET",
        f"/resources/{RID}/elements/def-st",
        json=[{"meta": True}, {"kerml:esiData": {"name": "status"}}],
    )

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    item = adapter.get("101")
    assert item.fields == {"name": "req", "status": "draft"}
    assert item.type == "requirement"


# ---------------------------------------------------------------- writes


def test_create_returns_native_id(env):
    _add_login_logout(env)
    _add_project(env)
    env.add(
        "POST",
        f"/resources/{RID}/elements",
        body={"name": "new", "properties": {"specification": "text"}, "type": "requirement"},
        json={"id": 555},
    )

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    from websync.models import SyncItem

    new_id = adapter.create(
        SyncItem(native_id="", type="requirement", fields={"name": "new", "specification": "text"})
    )
    assert new_id == "555"


def test_update_patches_element(env):
    _add_login_logout(env)
    _add_project(env)
    env.add(
        "PATCH",
        f"/resources/{RID}/elements/101",
        body={"name": "renamed", "properties": {"status": "approved"}},
        json={},
    )

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    adapter.update("101", {"name": "renamed", "status": "approved"})
    patch = env.calls_for("PATCH", f"/resources/{RID}/elements/101")[0]
    assert patch["body"] == {"name": "renamed", "properties": {"status": "approved"}}


# ---------------------------------------------------------------- robustness


def test_401_mid_run_relogins_and_retries(env):
    _add_login_logout(env)
    env.add("GET", RESOURCES_PATH, status=401)  # session expired
    env.add("GET", RESOURCES_PATH, json=_resources())

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    adapter.healthcheck()
    # a fresh session was created for the retry -> a second login
    assert len(env.calls_for("GET", "/login")) == 2


def test_5xx_retried_then_succeeds(env):
    _add_login_logout(env)
    env.add("GET", RESOURCES_PATH, status=500)
    env.add("GET", RESOURCES_PATH, json=_resources())

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    adapter.healthcheck()
    assert len(env.calls_for("GET", RESOURCES_PATH)) == 2


def test_persistent_5xx_raises_transient(env):
    _add_login_logout(env)
    env.add("GET", RESOURCES_PATH, status=500, repeat=3)

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    with pytest.raises(TransientTWCError, match="HTTP 500"):
        adapter.healthcheck()


def test_client_error_raises_sync_error(env):
    _add_login_logout(env)
    _add_project(env)
    env.add("GET", f"/resources/{RID}/elements/missing", status=404, json={"error": "nope"})

    adapter = _no_retry(CameoTWCAdapter.from_env(_scope()))
    with pytest.raises(SyncError, match="HTTP 404"):
        adapter.get("missing")
