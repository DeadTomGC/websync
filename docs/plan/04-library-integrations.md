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
| `requests` + `tenacity` | HTTP + retries/backoff | All custom adapters |
| `python-dotenv` | Local `.env` secrets | CI uses real env vars; this is dev convenience |
| `jinja2` | Markdown report templates | Reporting artifacts |
| `pytest`, `responses`, `ruff` | Test/lint stack | `responses` mocks `requests` in adapter tests |

All addresses/ports are configurable via `<TOOL>_BASE_URL` env vars
(e.g., `https://cameo.internal.example.com:8443/twc/v1`) — nothing is
hardcoded; internal servers use custom ports and sometimes self-signed certs,
so adapters accept an `INSECURE_SKIP_VERIFY` env var for staging.

## Per-tool assessment

### Cameo Teamwork Cloud (requirements) — custom thin client

- **Library:** none maintained exists on PyPI. (3rd-party wrappers like
  `pyTWC` are stale/hobby projects — do not adopt; we'd inherit their bugs.)
- **Approach:** `requests` client against TWC's REST API (`/twc/v1/...`):
  auth (form login → session token), project lookup, element query/update.
  TWC also exposes OSLC; prefer REST where CRUD is simpler, OSLC for
  requirements-link queries if needed.
- **Risk:** version differences between TWC installs. Mitigation: adapter
  targets the REST API version declared in the project config
  (`api_version:`), and `healthcheck()` verifies reachability + version
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
2. Retries with backoff (tenacity) on 5xx/timeouts; honors `Retry-After`.
3. Pagination on every list endpoint.
4. Timeouts on every request (never hang a CI job).
5. Structured logging of every request (URL, status, duration) at debug level.
6. `respx`-based contract tests with fixtures captured from real responses.
