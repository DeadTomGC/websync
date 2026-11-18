# Cameo Teamwork Cloud — adapter notes

Adapter: `src/websync/adapters/cameo_twc.py` (thin `requests` client — no
maintained upstream Python library exists; see `docs/plan/04-library-integrations.md`).

## Environment

| Variable | Meaning |
|---|---|
| `CAMEO_TWC_BASE_URL` | Full API root incl. custom port, e.g. `https://cameo.internal:8443/twcloud/rest/v1` |
| `CAMEO_TWC_USER` / `CAMEO_TWC_PASSWORD` | Credentials |
| `CAMEO_TWC_AUTH_PATH` | Override if your TWC version exposes auth elsewhere (default `/authentication`) |
| `CAMEO_TWC_INSECURE_SKIP_VERIFY` | `true` for staging with self-signed certs (loudly discouraged in production) |
| `CAMEO_TWC_TIMEOUT` | Per-request timeout seconds (default 30) |

## Auth model

1. `POST {base}/authentication` with HTTP Basic credentials.
2. Response carries a token under one of the keys `token` / `jwt` / `access_token`.
3. Subsequent requests send `Authorization: Bearer <token>`; on a 401 the
   adapter re-authenticates once and retries.

## Endpoints & payloads (as implemented)

| Operation | Request |
|---|---|
| List projects | `GET /projects` → `{"projects": [{"id", "name"}]}` (or bare list) |
| List elements | `GET /projects/{pid}/elements?page=N` → `{"elements": [...], "page": {"total", "size"}}` |
| Get element | `GET /elements/{id}` |
| Create | `POST /projects/{pid}/elements` body `{"name", "type", "properties": {...}}` → `{"id": ...}` |
| Update | `PATCH /elements/{id}` body `{"name"?, "properties": {...}}` |

Element JSON shape assumed by the adapter:

```json
{"id": 101, "name": "req 1", "type": "requirement",
 "modified": "2026-11-18T12:00:00Z", "properties": {"specification": "...", "status": "draft"}}
```

## Field conventions for mapping configs

- `name` maps to the element name; **every other native field is an element
  property key** (`specification`, `status`, `criticality`, ...).
- `scope.project`: numeric project ID (skips the `/projects` lookup) or the
  exact project name.
- `scope.item_type_filter`: optional list of element types to sync.
- `modified` timestamps are used only to *select* candidate items;
  classification/conflict detection is hash-based (see plan docs).

## VERIFY against your instance (before first production run)

TWC REST shapes differ between versions. Run
`pytest -m live tests/live/test_live_adapters.py -v` against a scratch
project and confirm:

- [ ] auth path and token response key,
- [ ] `/projects` response shape,
- [ ] element list payload (pagination keys `page.total` / `page.size`),
- [ ] create/update payload acceptance (does the server accept `properties` on PATCH?),
- [ ] the element `type` values used in `types:` and `item_type_filter`.

If anything differs, adjust the adapter's `_*_page` / `_to_payload` helpers —
they are intentionally isolated and unit-tested (`tests/test_cameo_twc.py`).
Record the verified shapes in this file when confirmed.
