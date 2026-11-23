# Library & API Integration Choices

Goal: minimize self-maintained code. Rule applied per tool: **use a
maintained upstream Python client if one exists; otherwise write the thinnest
possible `requests`-based adapter** limited to the endpoints we actually use,
and record API notes in `docs/tools/<tool>.md` as we learn them.

## Cross-cutting dependencies

| Package | Purpose | Notes |
|---|---|---|
| `typer` | CLI framework | Type-hint-driven; generates `--help` for free |
| `pydantic` (v2) | Models + YAML config validation | Single source of schema truth |
| `pyyaml` | Config loading | Loaded *through* pydantic, never raw |
| `atwc` | Teamwork Cloud REST client | Upstream lib for the Cameo TWC adapter (see below); brings `aiohttp` with it |
| `requests` + `tenacity` | HTTP + retries/backoff | Custom adapters not covered by a library (Jama) |
| `python-dotenv` | Local `.env` secrets | CI uses real env vars; this is dev convenience |
| `jinja2` | Markdown report templates | Reporting artifacts |
| `pytest`, `responses`, `ruff` | Test/lint stack | `responses` mocks `requests` in adapter tests |

All addresses/ports are configurable via `<TOOL>_BASE_URL` env vars
(e.g., `https://cameo.internal.example.com:8443/twc/v1`) — nothing is
hardcoded; internal servers use custom ports and sometimes self-signed certs,
so adapters accept an `INSECURE_SKIP_VERIFY` env var for staging.

## Per-tool assessment

### Cameo Teamwork Cloud (requirements) — use the `atwc` package

- **Library:** **`atwc`** (PyPI, Apache-2.0, maintained by Archimedes
  Exhibitions GmbH — https://github.com/amdx/atwc). Replaces the
  internally written thin `requests` client for this tool. (Other 3rd-party
  wrappers like `pyTWC` remain stale/hobby projects — still do not adopt.)
- **What it provides:** an asyncio/`aiohttp` client (`atwc.client.Client`)
  with HTTP Basic auth against TWC's `login`/`logout` endpoints (cookie-jar
  session, `create_session()` context manager) and generic `get`/`post`
  helpers, plus `ResourceBrowser` (project/resource tree, category paths)
  and `ModelBrowser` (revisions, element fetch, batch element fetch, tagged
  values, qualified names).
- **Adapter approach:** `adapters/cameo_twc.py` becomes glue around `atwc`
  rather than a from-scratch HTTP client. Because the sync engine is
  synchronous, the adapter bridges via `asyncio.run()` internally; the
  `Adapter` Protocol stays sync.
- **Gaps we must cover in the adapter (atwc does not provide them):**
  - **Writes:** `atwc` is read-focused (built for a Cameo Collaborator
    publisher). `create`/`update` go through its generic `client.post()`
    against the TWC REST endpoints we need — those calls, payload shapes,
    and pagination live in our adapter and are the main thing to verify
    against our TWC install.
  - **Robustness:** no retries/backoff, no explicit timeouts, no
    `Retry-After` handling. Wrap `atwc` calls with timeouts and retry-once
    logic in the adapter to meet the adapter quality bar below.
  - **Session/auth errors:** `atwc` raises on non-2xx
    (`raise_for_status=True`); map its exceptions to our `SyncError`
    hierarchy.
- **Auth/env:** `CAMEO_TWC_BASE_URL` is the TWC API root (e.g.
  `https://twc.internal:8111/osmc/`); `CAMEO_TWC_USER` /
  `CAMEO_TWC_PASSWORD` feed `atwc.client.Client(api_url, login, password)`.
  The old `CAMEO_TWC_AUTH_PATH` override is obsolete (login path is fixed
  relative to the base URL in `atwc`); `INSECURE_SKIP_VERIFY` for
  self-signed certs must be applied when constructing the `aiohttp` session
  in our adapter wrapper.
- **Risk:** `atwc` is a small, niche upstream (it self-describes as an
  interface abstraction for one company's publisher, "not intended as a
  general-purpose solution"). Mitigations: pin the version in
  `pyproject.toml`; keep all `atwc` usage behind `adapters/cameo_twc.py`
  so a breaking upstream change is contained; contract tests
  (local stub HTTP server, `tests/httpstub.py`) plus `tests/live/`
  verification against
  a scratch project; if upstream turns out unusable for our TWC version,
  the adapter is the only module to rewrite (falling back to a thin client
  is a contained change).
- **Risk:** version differences between TWC installs remain. Mitigation:
  `api_version` in config; `healthcheck()` verifies reachability + version
  before each run and logs it.

### Jama (requirements, epics, stories) — custom thin client

- **Library:** community wrappers (`pyJama` etc.) are unmaintained; Jama
  itself ships no official Python client.
- **Approach:** `requests` client for the documented **Jama REST API v1**
  (`/rest/v1/items`, `/rest/v1/abstractitems`, relationships endpoints,
  `/rest/v1/projects/{id}/items` with pagination). OAuth2 client-credentials
  flow using `JAMA_CLIENT_ID`/`JAMA_CLIENT_SECRET`; token refresh on 401.
- **Perks:** Jama has a websocket/stream activity API, but Phase 1 uses
  last-sync-timestamp queries; note the activity stream as a future
  optimization in `docs/tools/jama.md`.

### Jira (epics/stories) — use the `jira` PyPI library

- **Library:** **`jira`** (officially maintained by Py.jira community, ~3.6+)
  — mature, typed, covers auth (PAT/bearer), JQL search, issue CRUD, custom
  fields, pagination, and Epic link fields. This is our biggest code-saving
  win; the adapter should be mostly glue.
- **Auth:** `JIRA_TOKEN` (personal access / API token) + `JIRA_USER`.
- **Note:** Epic link is a custom field (`customfield_*` in company-managed
  projects; the `epic` parent field in team-managed). Resolve the concrete
  field ID per project at adapter init, not in config.

### Teamcenter (future, releases) — plan only

- No public Python client. Siemens exposes **AWC REST APIs** (Active
  Workspace) / ESOA (SOA web services). REST-first: authenticate, upload
  dataset files (release artifacts), attach to a released Item Revision.
- Before implementation, write `docs/tools/teamcenter.md` from your
  instance's available REST endpoints (installations vary heavily). The
  adapter pattern stays identical to the others.

### Windchill (future, releases) — plan only

- PTC ships **Windchill REST Services (WRS)** — OData/REST, documented
  Swagger per version. Upload content/attachments to a Product/Change
  context. Again: document endpoints first in `docs/tools/windchill.md`,
  then a thin adapter.

## Adapter quality bar (every adapter, no exceptions)

1. Implements the full `Adapter` Protocol (see `02-implementation-guide.md`).
2. Retries with backoff (tenacity or equivalent) on 5xx/timeouts; honors
   `Retry-After`. (For `atwc`-backed calls, implement this in the adapter —
   upstream provides none.)
3. Pagination on every list endpoint.
4. Timeouts on every request (never hang a CI job).
5. Structured logging of every request (URL, status, duration) at debug level.
6. Contract tests with fixtures captured from real responses: `responses`
   for `requests`-based adapters (Jama); for the `atwc`/`aiohttp`-based
   TWC adapter, a small local stub HTTP server (`tests/httpstub.py`).
   (`aioresponses`, the natural choice, is incompatible with the
   aiohttp ≥ 3.13 wheels required on Python 3.15 — `ClientResponse`
   signature change — and older aiohttp cannot be built on this platform;
   the stub server gives the same properties while exercising the real
   aiohttp + atwc stack.)
