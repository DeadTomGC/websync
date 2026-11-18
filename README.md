# websync

Synchronizes engineering items across internal tools — **Cameo Teamwork
Cloud**, **Jama**, and **Jira** today, with **Teamcenter** and **Windchill**
release publishing planned — as a one-shot CLI run from GitLab CI pipelines.

- Architecture, decisions, and roadmap: [`docs/plan/`](docs/plan/)
- Mapping config schema: [`docs/plan/03-mapping-configuration-spec.md`](docs/plan/03-mapping-configuration-spec.md)
- Example config: [`configs/programA.cameo-jama.yaml`](configs/programA.cameo-jama.yaml)

## Quick start

```bash
git clone <repo> && cd web_integrations

# All execution happens in a venv — never install packages globally.
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # add ",jira" extra in Phase 3

ruff check src tests           # lint
pytest                          # run the test suite (no network needed)

# Validate a mapping config (same command CI runs on merge requests):
websync validate-config configs/*.yaml

# Sync a project (arrives in Phase 1):
websync run --config configs/programA.cameo-jama.yaml --dry-run
```

## Configuration

Per-project field/type mappings are versioned YAML files in `configs/`.
Secrets and server addresses (including custom ports) come **only** from
environment variables — see [`.env.example`](.env.example). In GitLab CI they
are CI/CD variables; locally, copy it to `.env` (gitignored).

## Project status

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo, venv, CI, CLI skeleton, config schema | ✅ done |
| 1 | Sync engine, SQLite state, mapping layer, reports | ✅ done |
| 2 | Cameo TWC ↔ Jama requirements | planned |
| 3 | Jama ↔ Jira epics/stories | planned |
| 4 | Hardening / scheduled runs | planned |
| 5–6 | Teamcenter & Windchill releases | future |

## Repo layout

```
configs/       one mapping YAML per program/project
docs/plan/     the design documents that guide implementation
src/websync/   CLI, config schema, models; adapters/ holds tool clients
tests/         unit tests (CI, no network) ; live tests come with adapters
```
