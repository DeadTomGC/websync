# Jama Connect — adapter notes

Adapter: `src/websync/adapters/jama.py` (thin client for the documented
**REST API v1**; community Python wrappers are unmaintained — see
`docs/plan/04-library-integrations.md`).

## Environment

| Variable | Meaning |
|---|---|
| `JAMA_BASE_URL` | e.g. `https://jama.internal.example.com` (custom ports fine) |
| `JAMA_CLIENT_ID` / `JAMA_CLIENT_SECRET` | OAuth2 client-credentials app (create in Jama's Admin > Apps) |
| `JAMA_INSECURE_SKIP_VERIFY` | `true` for staging with self-signed certs |
| `JAMA_TIMEOUT` | Per-request timeout seconds (default 30) |

## Auth model

1. `POST /rest/oauth/token` with HTTP Basic (`client_id:secret`) and
   `grant_type=client_credentials` → `{"access_token", "expires_in"}`.
2. Bearer token on subsequent requests; 401 triggers re-auth + single retry.

## Endpoints & payloads (as implemented)

| Operation | Request |
|---|---|
| Healthcheck | `GET /rest/v1/users/current` |
| List items | `GET /rest/v1/items?project={id}&startAt=N&maxResults=50` (paginates via `meta.pageInfo`) |
| Get item | `GET /rest/v1/items/{id}` |
| Create | `POST /rest/v1/items` `{"project", "itemTypeId", "name", "description"}` → `{"data": {"id"}}` |
| Patch fields | `PATCH /rest/v1/items/{id}` `{"name"?, "description"?, "location": {"fieldData": {...}}}` |
| Item types | `GET /rest/v1/itemtypes`, `GET /rest/v1/itemtypes/{id}` (key/label resolution, cached) |

## Field conventions for mapping configs

- **Core fields**: `name`, `description` — top-level item fields.
- **Custom fields** use dotted native names: `fieldData.<filterId>.<fieldId>`.
  The adapter flattens `location.fieldData` into those keys on read and nests
  them back on write. Find filter/field IDs in Jama's item type configuration
  (or via the API: `GET /rest/v1/itemtypes/{id}` → `fields`).
- **Types**: `types:` and `item_type_filter:` use the item type's **key**
  (e.g. `requirement`) or numeric ID; the adapter resolves via
  `/rest/v1/itemtypes` and caches.
- `scope.project` **must be the numeric Jama project/container ID** (the
  adapter rejects names to avoid ambiguous lookups).
- Change filtering uses `lastActivityDate` client-side; final classification
  is hash-based (see plan docs), so server clock drift is harmless.

## VERIFY against your instance (before first production run)

Run `pytest -m live tests/live/test_live_adapters.py -v` (needs
`JAMA_LIVE_PROJECT=<numeric id>` set) against a scratch project and confirm:

- [ ] OAuth client-credentials flow works with your app credentials,
- [ ] items list pagination params (`startAt`/`maxResults`) match your version,
- [ ] custom-field PATCHes via `location.fieldData` are accepted,
- [ ] item type keys used in configs match `GET /rest/v1/itemtypes` output.

Record verified shapes here when confirmed.
