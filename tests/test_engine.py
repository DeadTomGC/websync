"""Engine tests using in-memory fake adapters (no network).

Covers the Phase 1 definition of done:
create/update/conflict/no-op in both directions, idempotent reruns,
dry runs leaving state untouched, direction restrictions, and error counting.
"""

from __future__ import annotations

import textwrap
from datetime import UTC

import pytest

from websync.adapters.fake import InMemoryAdapter
from websync.config import ProgramConfig
from websync.engine import SyncEngine
from websync.state import StateStore


def make_config(yaml_text: str) -> ProgramConfig:
    import yaml

    return ProgramConfig.model_validate(yaml.safe_load(textwrap.dedent(yaml_text)))


BASE_CONFIG = """
program: prog
links:
  - name: link-a-b
    source: toolA
    target: toolB
    types:
      requirement: Requirement
    scope:
      source: {project: A}
      target: {project: B}
    fields:
      title: {source: name, target: name}
      text: {source: desc, target: description}
    conflict_strategy: skip_and_report
"""


@pytest.fixture
def env(tmp_path):
    """A run environment: state db + two fake adapters per tool."""
    state = StateStore(tmp_path / "state.db")
    adapters = {"toolA": InMemoryAdapter(), "toolB": InMemoryAdapter()}
    return state, adapters, tmp_path


def run_once(env, config, dry_run=False, link=None):
    state, adapters, tmp_path = env
    engine = SyncEngine(
        config,
        adapters,
        state,
        config_path="cfg.yaml",
        dry_run=dry_run,
        reports_dir=str(tmp_path / "reports"),
    )
    return engine.run(link_filter=link)


# ------------------------------------------------------------------ creates


def test_new_source_item_creates_target_counterpart(env):
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "new req", "desc": "text"})
    results = run_once(env, make_config(BASE_CONFIG))
    assert results[0]["stats"]["created"] == 1
    created_id = _only_target_id(adapters["toolB"])
    assert adapters["toolB"].fields_of(created_id) == {
        "name": "new req",
        "description": "text",
    }


def test_new_target_item_creates_source_counterpart_bidirectional(env):
    _, adapters, _ = env
    adapters["toolB"].seed("B1", "Requirement", {"name": "from target", "description": "x"})
    results = run_once(env, make_config(BASE_CONFIG))
    assert results[0]["stats"]["created"] == 1
    created = _only_target_id(adapters["toolA"])
    assert adapters["toolA"].fields_of(created) == {
        "name": "from target",
        "desc": "x",
    }


def test_create_missing_false_skips(env):
    config = make_config(
        BASE_CONFIG + "    create_missing: false\n    direction: source_to_target\n"
    )
    state, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "r"})
    results = run_once(env, config)
    assert results[0]["stats"]["created"] == 0
    assert results[0]["stats"]["skipped"] == 1


# ------------------------------------------------------------------ updates


def test_source_update_pushes_to_target(env):
    _, adapters, _ = env
    # First run creates the pair.
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, make_config(BASE_CONFIG))
    # Now change the source item only.
    adapters["toolA"].update("A1", {"name": "v2"})
    results = run_once(env, make_config(BASE_CONFIG))
    assert results[0]["stats"]["updated"] == 1
    target_id = _only_target_id(adapters["toolB"])
    assert adapters["toolB"].fields_of(target_id)["name"] == "v2"


def test_target_update_pushes_back_to_source(env):
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, make_config(BASE_CONFIG))  # create pair
    target_id = _only_target_id(adapters["toolB"])
    adapters["toolB"].update(target_id, {"description": "changed"})
    results = run_once(env, make_config(BASE_CONFIG))
    assert results[0]["stats"]["updated"] == 1
    assert adapters["toolA"].fields_of("A1")["desc"] == "changed"


def test_noop_rerun_is_idempotent(env):
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    first = run_once(env, make_config(BASE_CONFIG))
    assert first[0]["stats"]["created"] == 1
    target_id = _only_target_id(adapters["toolB"])
    before = dict(adapters["toolB"].fields_of(target_id))
    modified = adapters["toolB"]._modified[target_id]
    # Re-fetch happens (overlap margin) but hashes match -> no writes.
    second = run_once(env, make_config(BASE_CONFIG))
    assert second[0]["stats"]["updated"] == 0
    assert second[0]["stats"]["created"] == 0
    assert adapters["toolB"].fields_of(target_id) == before
    assert adapters["toolB"]._modified[target_id] == modified


# ------------------------------------------------------------------ conflicts


def test_conflict_skip_and_report(env):
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, make_config(BASE_CONFIG))  # create pair
    target_id = _only_target_id(adapters["toolB"])
    # Both sides change before the next run.
    adapters["toolA"].update("A1", {"name": "src-change"})
    adapters["toolB"].update(target_id, {"name": "tgt-change"})
    results = run_once(env, make_config(BASE_CONFIG))
    stats = results[0]["stats"]
    assert stats["conflicts"] == 1
    assert stats["updated"] == 0
    # Neither side was overwritten.
    assert adapters["toolA"].fields_of("A1")["name"] == "src-change"
    assert adapters["toolB"].fields_of(target_id)["name"] == "tgt-change"
    # Conflict recorded against the run and in the report.
    state = env[0]
    report = state.get_report(results[0]["run_id"])
    assert "CONFLICT" in report


def test_run_with_conflicts_is_partial_and_does_not_advance_window(env):
    state, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    clean = run_once(env, make_config(BASE_CONFIG))[0]
    assert state.get_run(clean["run_id"])["status"] == "success"
    window = state.last_successful_started("link-a-b")
    target_id = _only_target_id(adapters["toolB"])
    adapters["toolA"].update("A1", {"name": "src-change"})
    adapters["toolB"].update(target_id, {"name": "tgt-change"})
    res = run_once(env, make_config(BASE_CONFIG))[0]
    assert state.get_run(res["run_id"])["status"] == "partial"
    assert state.last_successful_started("link-a-b") == window


def test_conflict_newest_wins(env):
    config = make_config(BASE_CONFIG.replace("skip_and_report", "newest_wins"))
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, config)
    target_id = _only_target_id(adapters["toolB"])
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    adapters["toolA"].update("A1", {"name": "src-change"})
    adapters["toolB"].update(target_id, {"name": "tgt-change"})
    # Backdate target's modification so source is newer.
    adapters["toolB"]._modified[target_id] = now - timedelta(hours=1)
    results = run_once(env, config)
    assert results[0]["stats"]["updated"] == 1
    assert adapters["toolB"].fields_of(target_id)["name"] == "src-change"


def test_conflict_source_wins(env):
    config = make_config(BASE_CONFIG.replace("skip_and_report", "source_wins"))
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, config)
    target_id = _only_target_id(adapters["toolB"])
    adapters["toolA"].update("A1", {"name": "src-change"})
    adapters["toolB"].update(target_id, {"name": "tgt-change"})
    run_once(env, config)
    assert adapters["toolB"].fields_of(target_id)["name"] == "src-change"
    assert adapters["toolA"].fields_of("A1")["name"] == "src-change"


def test_conflict_target_wins(env):
    config = make_config(BASE_CONFIG.replace("skip_and_report", "target_wins"))
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, config)
    target_id = _only_target_id(adapters["toolB"])
    adapters["toolA"].update("A1", {"name": "src-change"})
    adapters["toolB"].update(target_id, {"name": "tgt-change"})
    run_once(env, config)
    assert adapters["toolA"].fields_of("A1")["name"] == "tgt-change"


# ------------------------------------------------------------------ direction


def test_source_to_target_ignores_target_changes_but_reports_nothing(env):
    config = make_config(BASE_CONFIG + "    direction: source_to_target\n")
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    run_once(env, config)  # create pair (s2t allows create)
    target_id = _only_target_id(adapters["toolB"])
    adapters["toolB"].update(target_id, {"name": "rogue"})
    results = run_once(env, config)
    stats = results[0]["stats"]
    # Target-side change is detected but never written to the source.
    assert stats["updated"] == 0
    assert stats["skipped"] == 1
    assert adapters["toolA"].fields_of("A1")["name"] == "v1"


# ------------------------------------------------------------------ dry run


def test_dry_run_writes_neither_tools_nor_id_map(env):
    state, adapters, _ = env
    adapters["toolA"].seed("A1", "requirement", {"name": "v1", "desc": "d"})
    results = run_once(env, make_config(BASE_CONFIG), dry_run=True)
    assert results[0]["stats"]["created"] == 1
    assert adapters["toolB"]._next_id == 0  # nothing created
    # No pair recorded, and no successful run recorded.
    assert state.get_pair("link-a-b", "source", "A1") is None
    assert state.last_successful_started("link-a-b") is None
    # Report exists and marks the dry run.
    report = state.get_report(results[0]["run_id"])
    assert "dry run" in report


# ------------------------------------------------------------------ run guard


def test_second_run_for_same_link_is_refused(env):
    state, _, _ = env
    # Simulate an in-flight run: insert a running row directly.
    state.start_run("prog", "cfg.yaml", "link-a-b", dry_run=False)
    results = run_once(env, make_config(BASE_CONFIG))
    # The engine records a skipped/failed result instead of crashing.
    assert len(results) == 1
    assert results[0]["stats"].get("errors") == 1
    assert results[0]["run_id"] is None


# ------------------------------------------------------------------ misc


def test_type_filter_skips_unmapped_types(env):
    _, adapters, _ = env
    adapters["toolA"].seed("A9", "risk", {"name": "not mapped"})
    results = run_once(env, make_config(BASE_CONFIG))
    assert results[0]["stats"]["created"] == 0
    assert results[0]["stats"]["skipped"] == 1


def test_link_filter(env):
    config = make_config(
        """
        program: prog
        links:
          - name: link-a-b
            source: toolA
            target: toolB
            types: {requirement: Requirement}
            scope: {source: {project: A}, target: {project: B}}
            fields: {title: {source: name, target: name}}
          - name: other
            source: toolA
            target: toolB
            types: {risk: Risk}
            scope: {source: {project: A}, target: {project: B}}
            fields: {title: {source: name, target: name}}
        """
    )
    _, adapters, _ = env
    adapters["toolA"].seed("A1", "risk", {"name": "risk item"})
    results = run_once(env, config, link="link-a-b")
    assert len(results) == 1
    assert results[0]["link"] == "link-a-b"


def _only_target_id(adapter: InMemoryAdapter) -> str:
    return f"fake-{adapter._next_id}"
