# Cameo Teamwork Cloud — adapter notes

Adapter: `src/websync/adapters/cameo_twc.py`, built on the **`atwc`**
package (PyPI, Apache-2.0, https://github.com/amdx/atwc) instead of an
internally written HTTP client. Rationale and gap analysis live in
`docs/plan/04-library-integrations.md`. Contract tests:
`tests/test_cameo_twc.py` (atwc/aiohttp against a local stub HTTP server,
`tests/httpstub.py`).

## Environment

| Variable | Meaning |
|---|---|
| `CAMEO_TWC_BASE_URL` | TWC REST API root incl. custom port, e.g. `https://twc.internal:8111/osmc/` — passed to `TWCClient` (an `atwc.client.Client` subclass) |
| `CAMEO_TWC_USER` / `CAMEO_TWC_PASSWORD` | Credentials → HTTP Basic auth (`aiohttp.BasicAuth`, as in atwc) |
| `CAMEO_TWC_INSECURE_SKIP_VERIFY` | `true` for staging with self-signed certs (loudly discouraged in production); the client builds its `aiohttp` session with `ssl=False` |
| `CAMEO_TWC_TIMEOUT` | Per-request timeout seconds (default 30), as `aiohttp.ClientTimeout` — atwc sets none itself |

There is no `CAMEO_TWC_AUTH_PATH` anymore: atwc authenticates against
`{base}/login` and `{base}/logout`, fixed relative to the base URL.

## Auth model

`TWCClient` (in the adapter module) overrides atwc's `create_session()`:

1. `GET {base}/login` with HTTP Basic credentials; **must return 204**.
   atwc's own login json-parses the empty 204 body, which always raises on
   a real server — our override reads only the status (VERIFY: our TWC's
   login is indeed a bare 204).
2. The session keeps the returned cookie; all further calls are
   authenticated with it.
3. On exit: `GET {base}/logout` (failures logged at debug, never fatal).
4. Each adapter operation is one complete session
   (`asyncio.run()`-bridged). A **401 mid-run is retried with a fresh
   session**, i.e. re-authenticate-once; a 401 on *login* itself maps to
   `AuthError` (bad credentials) and aborts.

## Robustness the adapter adds (atwc provides none of it)

- request timeouts (`aiohttp.ClientTimeout`, `CAMEO_TWC_TIMEOUT`),
- TLS verification control (`CAMEO_TWC_INSECURE_SKIP_VERIFY`),
- retries: up to 3 attempts with exponential backoff on 5xx/429,
  connection errors and timeouts; non-idempotent retry safety does not
  apply here since each attempt re-sends only within a fresh session,
- error mapping: `atwc`/`aiohttp` exceptions → `SyncError` /
  `AuthError` / `TransientTWCError`,
- batched traversal (`batch_size`, default 50 elements per batch fetch).

## Endpoints & payloads (as implemented)

| Operation | Request |
|---|---|
| List resources | `GET /resources?includeBody=true` → list of `{"ID", "dcterms:title", "categoryID", ...}` (atwc `ResourceBrowser`) |
| List revisions | `GET /resources/{rid}/revisions` → list; first entry is the current revision |
| Revision data | `GET /resources/{rid}/revisions/{rev}` → list whose `[0]` holds `rootObjectIDs` |
| Batch element read | `POST /resources/{rid}/elements` body `["<id>", ...]` → `{id: {"data": [meta, element]}}` (atwc `ModelBrowser.get_elements_batch`) |
| Single element read | `GET /resources/{rid}/elements/{id}` → list whose `[1]` is the element (atwc `ModelBrowser.get_element`) |
| Create | `POST /resources/{rid}/elements` body `{"name", "type", "properties": {...}}` → `{"id": ...}` |
| Update | `PATCH /resources/{rid}/elements/{id}` body `{"name"?, "properties": {...}}` |

There is no "list all elements" endpoint: `fetch_changed` **traverses the
model** from the revision's `rootObjectIDs`, following every `@id`
reference in each element's `kerml:esiData`, batch-fetching as it goes.
Changes are then filtered client-side by `modified` timestamps (server
clocks are untrusted; classification/conflict detection stays hash-based).

## Field conventions for mapping configs

- `SyncItem.type` = element `@type` with namespace prefix stripped
  (e.g. `kerml:Requirement` → `Requirement`); elements without a type
  (tagged values, sections, other structural values) are never items.
- `SyncItem.fields`: `name` = element name (from `kerml:esiData.name`);
  every other key is a **tagged value** (tag-definition name → value, via
  atwc's tagged-value machinery, with tag definitions cached per run).
- `scope.project`: resource ID or title — `Name` and `Name.MASTER` both
  match, `.MASTER` preferred when ambiguous.
- `scope.item_type_filter`: optional list of element types to sync.

## VERIFY against your instance (before first production run)

`atwc` was written against one company's TWC install ("not intended as a
general-purpose solution"), so shapes may differ on ours. Run
`pytest -m live tests/live/test_live_adapters.py -v` against a scratch
project and confirm:

- [ ] `GET {base}/login` returns a bare 204 with Basic auth (and logout too),
- [ ] `GET /resources?includeBody=true` payload shape (`ID`,
      `dcterms:title`, `.MASTER`/`.CC` suffixes) matches,
- [ ] `GET /resources/{rid}/revisions` + `.../revisions/{rev}` payload
      shapes (atwc reads `rootObjectIDs` from `revdata[0]`, assumes scalar
      revision IDs),
- [ ] element payload shapes (`result[1]` for single reads,
      `{id: {"data": [meta, element]}}` for batches, `kerml:esiData`
      structure, `@id` reference lists),
- [ ] the traversal reaches the model's requirement elements
      (the `@type`/stereotype values used in `types:` and
      `item_type_filter`),
- [ ] tagged-value payloads (`taggedValue` → `tagDefinition` → definition
      name) and the resulting field keys,
- [ ] write endpoints accept our create/update payloads via
      `TWCClient.post`/`.patch` — atwc itself never writes, so this is
      entirely our verification,
- [ ] self-signed cert handling (`INSECURE_SKIP_VERIFY` path) works.

Record the verified shapes in this file when confirmed. If `atwc` proves
unusable against our TWC version, fall back to a thin client — but only
`adapters/cameo_twc.py` changes (see risks in `docs/plan/05-roadmap.md`).
