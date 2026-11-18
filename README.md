# websync

Synchronizes engineering items across internal tools — **Cameo Teamwork
Cloud**, **Jama**, and **Jira** today, with **Teamcenter** and **Windchill**
release publishing planned — as a one-shot CLI run from GitLab CI pipelines.

- Architecture, decisions, and roadmap: [`docs/plan/`](docs/plan/)
- Implementation guide for contributors: [`docs/plan/02-implementation-guide.md`](docs/plan/02-implementation-guide.md)
- Mapping config schema: [`docs/plan/03-mapping-configuration-spec.md`](docs/plan/03-mapping-configuration-spec.md)
- Tool adapter notes (endpoints, auth, field conventions):
  [`docs/tools/cameo-twc.md`](docs/tools/cameo-twc.md) ·
  [`docs/tools/jama.md`](docs/tools/jama.md)
- Example config: [`configs/programA.cameo-jama.yaml`](configs/programA.cameo-jama.yaml)

## Setup

Prerequisites: Python **3.11+** and git. Everything runs inside a venv —
never install packages globally.

```bash
git clone <repo> && cd web_integrations

python3 -m venv .venv
source .venv/bin/activate          # required for every shell/session (incl. CI jobs)
pip install -e ".[dev]"            # app + pytest, ruff, responses; add ",jira" from Phase 3 on

cp .env.example .env               # optional, local secrets only (.env is gitignored)
```

Tool credentials and server addresses (including custom ports) come **only**
from environment variables, listed in [`.env.example`](.env.example). In
GitLab CI they are CI/CD variables; locally put them in `.env`. No secrets
are ever read from YAML configs.

## Testing

All commands run inside the activated venv. **No test requires network
access** — adapter tests mock HTTP with [`responses`](https://github.com/getsentry/responses).

```bash
ruff check src tests               # lint (CI enforces this too)
ruff format --check src tests      # formatting check
pytest -q                          # full suite (~90 tests, <10s)
pytest tests/test_engine.py -q     # just the sync engine
pytest --cov=websync               # with coverage
```

Live checks against real internal instances are excluded by default and are
run manually (Phase 2 exit criteria) — point at **scratch projects only**:

```bash
pytest -m live tests/live/test_live_adapters.py -v
# requires e.g. CAMEO_TWC_BASE_URL, CAMEO_TWC_LIVE_PROJECT,
#             JAMA_BASE_URL, JAMA_LIVE_PROJECT  (see .env.example)
```

CI (`.gitlab-ci.yml`) runs the same thing on every merge request:
`lint` → `test` → `validate-config` (validates every `configs/*.yaml`), with a
manual/scheduled `sync` job that publishes `reports/` as artifacts.

## Running

```bash
# Validate mapping configs — the same command CI runs on merge requests:
websync validate-config configs/*.yaml

# Sync a project (dry-run first when rolling out new mappings!):
websync run --config configs/programA.cameo-jama.yaml --dry-run
websync run --config configs/programA.cameo-jama.yaml

# Exercise the whole pipeline with in-memory adapters, no tools needed:
websync run --config configs/programA.cameo-jama.yaml --fake

# Inspect state (SQLite, path via --db or WEBSYNC_DB_PATH):
websync status
websync report           # prints the Markdown report of the latest run
```

Each run writes a Markdown report (per link) to `reports/` for CI artifacts,
records run history and conflicts in the state DB, and refuses to start if
the same link already has an active run.

## Project status

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo, venv, CI, CLI skeleton, config schema | ✅ done |
| 1 | Sync engine, SQLite state, mapping layer, reports | ✅ done |
| 2 | Cameo TWC ↔ Jama requirements | ✅ done — live check against a scratch project pending |
| 3 | Jama ↔ Jira epics/stories | planned |
| 4 | Hardening / scheduled runs | planned |
| 5–6 | Teamcenter & Windchill releases | future |

Roadmap details and exit criteria: [`docs/plan/05-roadmap.md`](docs/plan/05-roadmap.md).

## Repo layout

```
configs/          one mapping YAML per program/project (versioned, CI-validated)
docs/plan/        the design documents that guide implementation
docs/tools/       per-tool adapter notes (endpoints, auth, field conventions)
src/websync/      CLI, config schema, models, engine, state, reporting
src/websync/adapters/  tool clients (cameo_twc, jama, jira stub, fake, shared http)
tests/            unit + mocked-HTTP contract tests (CI, no network)
tests/live/       live adapter checks (manual only, marker 'live')
```
