# Implementation Guide for Future Agents

This document tells you exactly how to build the `websync` project described in
`01-overview-architecture.md`. Read that file first. Follow the phases in
order; each phase ends in a green pipeline.

## Ground rules

1. **Git repo:** `git init` the `web_integrations` directory on day one
   (default branch `main`). Commit docs and code together; conventional
   commits (`feat:`, `fix:`, `docs:`). All future changes go through branches
   and merge requests.
2. **venv everywhere:** Create `.venv` (`python3 -m venv .venv`) and install
   with `pip install -e ".[dev]"` inside it. CI and local docs must use the
   same invocation. Never install packages globally.
3. **Python version:** Pin the minimum version in `pyproject.toml`
   (3.11+ recommended). Run `ruff check` and `pytest` before every commit.
4. **Secrets:** Only via environment variables (see table below). Add
   `.env.example` documenting them. Never commit tokens; add `.env`,
   `state.db*`, `.venv/` to `.gitignore`.

## Environment variables (CI/CD variables per project)

| Variable | Used by |
|---|---|
| `CAMEO_TWC_BASE_URL`, `CAMEO_TWC_USER`, `CAMEO_TWC_PASSWORD` | Cameo TWC adapter |
| `JAMA_BASE_URL`, `JAMA_CLIENT_ID`, `JAMA_CLIENT_SECRET` | Jama adapter |
| `JIRA_BASE_URL`, `JIRA_USER`, `JIRA_TOKEN` | Jira adapter |
| `WEBSYNC_DB_PATH` | State DB location (CI cache/artifact mount) |

## Phase 0 — Scaffolding

- `pyproject.toml` with dependencies (see `04-library-integrations.md` for the
  full list and rationale), dev extras: `pytest`, `pytest-cov`, `ruff`,
  `responses` (HTTP mocking for `requests`), `mypy` optional.
- `src/websync/` skeleton: `cli.py` (typer app with `run`, `validate-config`,
  `status`, `report`), `models.py` (pydantic `SyncItem`, `Change`,
  `Conflict`), `adapters/base.py` (the `Adapter` Protocol + `SyncError`
  hierarchy), `state.py`, `mapping.py`, `engine.py`, `reporting.py`.
- `.gitlab-ci.yml` stages: `lint` (ruff), `test` (pytest), `sync` (manual or
  scheduled job running the CLI).
- A passing "hello" test and the pipeline green on all three stages.

## Phase 1 — Engine, state, mapping (tool-agnostic, fully unit-tested)

Build and test these with **fake adapters** before touching real tools:

- `state.py`: SQLite schema —
  `id_map(link_name, side, native_id, counterpart_id, last_synced_hash, PRIMARY KEY(link_name, side, native_id))`,
  `sync_runs(id, started, finished, config, stats_json)`,
  `conflicts(run_id, link_name, native_id, detail_json)`.
- `engine.py`: algorithm per run —
  1. record `sync_runs` row; load `since` = last successful run per link;
  2. fetch changed items from both sides (`fetch_changed`);
  3. map to normalized `SyncItem`s; compute diffs vs state hashes;
  4. classify each item: `create / update / conflict / no-op`;
  5. apply per conflict strategy from config; execute via adapters;
  6. update `id_map` + hashes; write run stats; emit report.
- `mapping.py`: pure, config-driven transforms. Field value coercions and
  type-map lookups must be data (YAML), not code, whenever possible.
- `reporting.py`: Markdown report written to `reports/` (CI artifact).
- `cli.py validate-config` loads a YAML and validates against the pydantic
  config model — this is what reviewers use to check a new project config.

**Definition of done:** full engine test suite passes using two in-memory fake
adapters, covering create/update/conflict/no-op/idempotent-rerun.

## Phase 2 — Cameo TWC ↔ Jama requirements

- Write `adapters/cameo_twc.py` first (see `docs/tools/cameo-twc.md` notes in
  `04-library-integrations.md` — no maintained Python lib exists, so this is
  a minimal `requests` client: auth → project → elements/requirements CRUD,
  with retries via `tenacity` and a `healthcheck()`).
- Write `adapters/jama.py` using the Jama REST API (`/rest/v1/items`,
  filters, `/sync` endpoints as needed) — also a thin client since
  community wrappers are unmaintained.
- Add `configs/` example: requirements link with field map (see
  `03-mapping-configuration-spec.md`).
- Integration tests: mock HTTP with `responses`; never require live servers in CI.
  Add `tests/live/` (skipped by default via marker) for manual verification
  against real internal instances.

## Phase 3 — Jama ↔ Jira epics/stories

- `adapters/jira.py` using the maintained `jira` PyPI library.
- Extend the same link machinery — **no engine changes should be needed**.
  If you find yourself modifying `engine.py` to support Jira, refactor
  instead: the engine is broken.
- Add Jama↔Jira epic/story mappings to the example config.

## Phase 4 (future) — Teamcenter / Windchill releases

- New CLI subcommand `publish-release --tool teamcenter|windchill`.
- Same adapter pattern; input = GitLab release artifacts. Document the
  Teamcenter (AWC REST) and Windchill (Windchill REST Services) endpoints in
  `docs/tools/` before writing adapters.

## Testing strategy

- **Unit:** engine, mapping, state — no network, fast, mandatory in CI.
- **Contract:** adapters against `respx`-mocked HTTP fixtures recorded from
  real API responses (capture during manual live checks).
- **Live:** `pytest -m live` run manually against internal servers with a
  scratch project; never against production data.
- Every new mapping config in `configs/` must ship with at least one fixture
  test exercising its transforms.

## Common pitfalls (read before debugging)

- **Clock skew:** internal servers may have drifting clocks; never compare
  raw timestamps across tools. Compare hashes stored in `id_map` instead,
  and use server timestamps only to *select* candidate changed items.
- **Auth:** Jama OAuth token expiry mid-run — adapters must refresh or
  re-auth on 401 and retry once.
- **Rate limits:** add polite defaults (small concurrency, retry after
  `Retry-After`).
- **Large changes:** paginate every `fetch` endpoint from day one.
