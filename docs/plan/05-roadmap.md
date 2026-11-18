# Roadmap & Phased Delivery

Each phase is independently deliverable and leaves the pipeline green.
"New agent" rows indicate a clean handoff point with no dangling work.

| Phase | Scope | Exit criteria |
|---|---|---|
| **0. Scaffold** | git repo, venv, pyproject, CI (lint/test/sync stages), CLI skeleton, `.gitlab-ci.yml`, `.env.example` | Pipeline green on empty CLI; repo tagged `v0.1.0` |
| **1. Core engine** | SQLite state, mapping layer, sync engine, conflict strategies, Markdown reports, `validate-config` — all against fake adapters | Full unit suite passes; dry-run demonstrated end-to-end with fakes |
| **2. Cameo TWC ↔ Jama** | TWC adapter (thin client), Jama adapter (thin client), first real config `configs/programA.cameo-jama.yaml`, live check against a scratch project, contract tests | Live dry-run clean against scratch project; real run creates/updates requirements idempotently |
| **3. Jama ↔ Jira** | Jira adapter (`jira` lib), epic/story link config, status/epic-link field mapping, rerun of program A config including both links | Epics + stories sync idempotently in both directions; conflict report verified with a deliberate edit collision |
| **4. Hardening** | Scheduled pipeline runs, SQLite state in CI cache/artifact, alerting on conflict-report growth, docs for config authors (`docs/config-authoring.md`) | Runs nightly unattended for two weeks without manual fixes |
| **5. Teamcenter releases** | `publish-release` subcommand, AWC REST adapter, endpoint docs, artifact upload from pipeline | Build pipeline publishes a release package to Teamcenter scratch workspace |
| **6. Windchill releases** | Windchill REST adapter, same subcommand with `--tool windchill` | Release published to Windchill scratch product context |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| TWC/Jama API version drift across internal installs | `api_version` in config; `healthcheck()` logs server version; adapters target documented REST versions |
| Rich-text mismatch mangles requirement bodies | Dedicated converters + round-trip property tests before Phase 2 exit |
| OAuth token expiry mid-run (Jama, TWC session) | Re-auth + single retry in adapters; integration test simulating 401 |
| CI job overwrites concurrent manual runs | SQLite write lock + `sync_runs` guard: refuse to start if another run for the same link is active |
| Config author mistakes hit production data | `validate-config` in CI, `--dry-run` default-on for new configs, scratch projects for all initial live testing |
| Self-signed internal certs break TLS | Per-tool `INSECURE_SKIP_VERIFY` env (staging only, warns loudly) |

## Definition of "maintainable" (acceptance criteria for the whole project)

- Adding a new tool = one adapter module + docs page; no engine changes.
- Adding/changing a mapping = YAML edit + merge request, no code.
- Every adapter has contract tests; CI never needs live servers.
- A new engineer can go from clone → green tests → successful fake-sync run
  in under 30 minutes using only `README.md`.
