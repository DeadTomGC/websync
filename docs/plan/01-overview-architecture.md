# Web Integrations Sync — Overview & Architecture

## Purpose

A maintainable Python CLI, run from GitLab CI pipelines, that synchronizes items
across multiple internal tools at custom addresses/ports:

- **Phase 1:** Requirements sync between **Cameo Teamwork Cloud** and **Jama**.
- **Phase 2:** Epics/Stories sync between **Jama** and **Jira**.
- **Future:** Push generated releases (GitLab build pipeline artifacts) to
  **Teamcenter** and **Windchill**.

Tool integrations will grow over time, so the architecture must make adding a new
tool or item type cheap. We maximize use of existing, maintained Python API
client libraries and keep our own code limited to adapters, mappings, and the
sync engine.

## Decisions (confirmed with stakeholders)

| Topic | Decision |
|---|---|
| Execution model | One-shot CLI invoked as a GitLab CI job (`sync run <link> --project <cfg>`) |
| State store | SQLite file (item ID mappings, last-sync timestamps, conflict log) |
| Per-project customization | Versioned YAML config files in the repo, one per program/project |
| Conflict handling | Configurable strategy per mapping + human-readable conflict report; never silently overwrite |

## High-level architecture

```
┌─────────────────────────── GitLab Pipeline ───────────────────────────┐
│  job: item-sync   (runs `websync run cameo-jama --config configs/A.yaml`) │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │      CLI layer (typer)  │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │      Sync Engine        │  core logic: change detection,
                    │  (tool-agnostic)        │  diffing, conflict resolution
                    └──┬───────────────┬──────┘
                       │               │
              ┌────────▼──────┐ ┌──────▼────────┐
              │ Cameo TWC     │ │ Jama adapter  │   Adapters wrap upstream
              │ adapter       │ │               │   Python libs where they
              │ (atwc lib)    │ │               │   exist (atwc, jira),
              └───────────────┘ └───────────────┘   thin `requests` clients
                       ┌───────────────┐            where they don't (Jama).
                       │ Jira adapter  │
                       └───────────────┘
   ┌──────────┐
   │ SQLite   │  ID mappings, sync runs, conflicts
   │ state.db │
   └──────────┘
```

### Core components

1. **CLI layer** — `typer` app with subcommands: `run`, `validate-config`,
   `status`, `report`. Secrets come from CI environment variables, never from
   the YAML files.
2. **Sync engine** (`src/websync/engine.py`) — completely tool-agnostic.
   Operates on a normalized `SyncItem` (pydantic model) with fields:
   `native_id`, `type`, `fields: dict`, `changed_at`, `version`. The engine
   never imports tool adapters directly; it receives them via a `LinkRunner`
   composed from the project config.
3. **Adapters** (`src/websync/adapters/`) — one per tool. Each implements the
   small `Adapter` protocol:
   - `fetch_changed(whence: datetime) -> list[SyncItem]`
   - `get(native_id) -> SyncItem`
   - `create(item: SyncItem) -> native_id`
   - `update(native_id, changes: dict)`
   - `healthcheck()`
   Adapters wrap official/community Python client libraries where available
   (`atwc` for Cameo TWC, `jira` for Jira); otherwise a minimal
   `requests`-based client scoped to exactly the endpoints we use (Jama).
4. **Mapping layer** (`src/websync/mapping.py`) — pure functions that translate
   `SyncItem` fields/types between two tools, driven entirely by the project
   YAML (see `03-mapping-configuration-spec.md`).
5. **State store** (`src/websync/state.py`) — SQLite via `sqlite3` (stdlib) or
   `SQLModel` if we outgrow it. Schema: `id_map`, `sync_runs`, `conflicts`.
6. **Conflict reporting** — every run emits a Markdown report artifact
   (GitLab CI `artifacts:`) listing created/updated/skipped/conflicted items.

### Adding a new tool later (Teamcenter, Windchill)

Because the engine only knows the `Adapter` protocol, adding a tool means:
write one adapter module, register it in `adapters/__init__.py`, add YAML
schema entries. No engine changes. Release-upload to Teamcenter/Windchill will
be a new CLI subcommand (`publish-release`) reusing the same adapter pattern,
fed by GitLab pipeline artifacts — designed for from the start.

## Repository layout

```
web_integrations/
├── pyproject.toml            # uv/pip venv, deps, ruff+pytest config
├── .gitlab-ci.yml            # pipeline: lint → test → run sync
├── configs/                  # one YAML per program/project (versioned)
│   └── programA.cameo-jama.yaml
├── docs/
│   ├── tools/cameo-twc.md    # per-tool API notes, auth quirks
│   ├── tools/jama.md
│   └── tools/jira.md
├── src/websync/
│   ├── cli.py  engine.py  mapping.py  state.py  models.py  reporting.py
│   └── adapters/{base.py, cameo_twc.py, jama.py, jira.py}
└── tests/
    ├── unit/                 # engine, mapping, state (no network)
    └── integration/          # adapter tests against recorded HTTP (vcr/respx)
```

## Operational notes

- **venv:** All development and CI execution uses a virtual environment
  (`.venv` created at repo setup; CI uses the same commands so local and
  pipeline behavior match).
- **Git:** Repo is initialized at first implementation commit; every doc and
  code change flows through merge requests so mapping configs get review.
- **Dry-run flag:** `sync run --dry-run` prints intended changes without
  writing — required for safe rollout of new mappings.
- **Idempotency:** Runs are restartable; each item operation is keyed by the
  ID mapping table so re-runs don't duplicate items.
