"""Engine behaviour with real adapters over mocked HTTP (wiring test)."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime

import responses

from websync.adapters.base import SyncError
from websync.adapters.cameo_twc import CameoTWCAdapter
from websync.adapters.jama import JamaAdapter
from websync.config import ProgramConfig
from websync.engine import SyncEngine
from websync.state import StateStore

NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)

CONFIG = """
program: prog
links:
  - name: cameo-jama
    source: cameo_twc
    target: jama
    types: {requirement: requirement}
    scope:
      source: {project: "Program A"}
      target: {project: 123}
    fields:
      title: {source: name, target: name}
      text: {source: specification, target: description}
    create_missing: true
"""


@responses.activate
def test_cameo_to_jama_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMEO_TWC_BASE_URL", "https://twc.example")
    monkeypatch.setenv("CAMEO_TWC_USER", "u")
    monkeypatch.setenv("CAMEO_TWC_PASSWORD", "p")
    monkeypatch.setenv("JAMA_BASE_URL", "https://jama.example")
    monkeypatch.setenv("JAMA_CLIENT_ID", "cid")
    monkeypatch.setenv("JAMA_CLIENT_SECRET", "secret")

    # --- Cameo side
    responses.post("https://twc.example/authentication", json={"token": "twc-tok"})
    responses.get(
        "https://twc.example/projects",
        json={"projects": [{"id": 7, "name": "Program A"}]},
    )
    responses.get(
        "https://twc.example/projects/7/elements",
        json={
            "elements": [
                {
                    "id": 101,
                    "name": "req 1",
                    "type": "requirement",
                    "modified": NOW.isoformat(),
                    "properties": {"specification": "body text"},
                }
            ]
        },
    )
    # --- Jama side
    responses.post("https://jama.example/rest/oauth/token", json={"access_token": "jama-tok"})
    responses.get("https://jama.example/rest/v1/users/current", json={"data": {"id": 1}})
    responses.get("https://jama.example/rest/v1/items", json={"data": [], "meta": {}})
    responses.get(
        "https://jama.example/rest/v1/itemtypes",
        json={"data": [{"id": 128, "key": "requirement", "label": "Requirement"}]},
    )
    responses.post("https://jama.example/rest/v1/items", json={"data": {"id": 551}})

    cfg = ProgramConfig.model_validate(__import__("yaml").safe_load(textwrap.dedent(CONFIG)))
    state = StateStore(tmp_path / "state.db")

    adapters = {
        "cameo_twc": CameoTWCAdapter.from_env(cfg.links[0].scope["source"]),
        "jama": JamaAdapter.from_env(cfg.links[0].scope["target"]),
    }
    engine = SyncEngine(cfg, adapters, state, reports_dir=str(tmp_path / "reports"))
    results = engine.run()

    stats = results[0]["stats"]
    assert stats["created"] == 1
    assert stats["errors"] == 0
    # the Jama create payload carried the mapped fields
    import json

    create_body = json.loads(
        [
            c
            for c in responses.calls
            if c.request.url.endswith("/rest/v1/items") and c.request.method == "POST"
        ][-1].request.body
    )
    assert create_body == {
        "project": 123,
        "itemTypeId": 128,
        "name": "req 1",
        "description": "body text",
    }
    # pair recorded in state
    pair = state.get_pair("cameo-jama", "source", "101")
    assert pair is not None and pair.target_id == "551"


@responses.activate
def test_unreachable_tool_marks_run_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMEO_TWC_BASE_URL", "https://twc.example")
    monkeypatch.setenv("CAMEO_TWC_USER", "u")
    monkeypatch.setenv("CAMEO_TWC_PASSWORD", "p")
    monkeypatch.setenv("JAMA_BASE_URL", "https://jama.example")
    monkeypatch.setenv("JAMA_CLIENT_ID", "cid")
    monkeypatch.setenv("JAMA_CLIENT_SECRET", "secret")

    responses.post("https://twc.example/authentication", json={"token": "twc-tok"})

    cfg = ProgramConfig.model_validate(__import__("yaml").safe_load(textwrap.dedent(CONFIG)))
    state = StateStore(tmp_path / "state.db")

    class DeadJama(JamaAdapter):
        def healthcheck(self) -> None:
            raise SyncError("jama", "connection refused (simulated)")

    adapters = {
        "cameo_twc": CameoTWCAdapter.from_env(cfg.links[0].scope["source"]),
        "jama": DeadJama.from_env(cfg.links[0].scope["target"]),
    }
    engine = SyncEngine(cfg, adapters, state, reports_dir=str(tmp_path / "reports"))
    results = engine.run()

    assert results[0]["stats"].get("errors") == 1
    run = state.get_run(results[0]["run_id"])
    assert run["status"] == "failed"
