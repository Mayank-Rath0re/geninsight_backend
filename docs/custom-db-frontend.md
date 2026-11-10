# Custom Databases — Frontend Integration Context

Everything the frontend needs to build the custom-database feature on top of the
current backend. Field names, status codes and response shapes are copied from the
implementation — treat this file as the contract.

---

## 1. Concepts

| Term | Meaning |
| --- | --- |
| **Data source** | One client-owned database connection (row in `data_sources`). Credentials are encrypted server-side and never returned. |
| **Registered table** | A remote table the user chose to expose. It becomes a normal dataset row (`table_info`) with a `table_info.id`; uploads and remote tables are indistinguishable to the rest of the app. |
| **Dataset** | `table_info` row. `GET /user_datasets` returns uploaded **and** remote tables. |
| **Session / dashboard** | Created against a dataset exactly like an uploaded file. Execution is routed to the correct database automatically. |

Supported engines: **MySQL, SQL Server, Postgres** (canonical `dbtype` values:
`mysql`, `mssql`, `postgres`).

Only base tables in the connection's **default schema** are exposed
(`DATABASE()` / `current_schema()` / `SCHEMA_NAME()`), up to
`CUSTOM_DB_MAX_TABLES_PER_SOURCE` (default **200**) tables per connection.

Nothing is copied into GenInsight; deleting a data source never touches the
client's remote data.

---

## 2. Auth & conventions

- All endpoints require `Authorization: Bearer <access_token>`.
- Every request body/query carries `userId`; it **must** match the token. A
  mismatch returns `401 {"detail": "User Mismatch"}`.
- Response timestamps are ISO-8601 strings or `null`.
- Add/edit/delete of a connection does **not** consume AI tokens and does not
  count toward the monthly query quota.

---

## 3. Endpoint overview

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/data_sources` | List the caller's connections + plan usage |
| `POST` | `/data_source` | Add a connection and register its tables |
| `POST` | `/update_data_source` | Edit connection details and/or exposed tables |
| `POST` | `/delete_data_source` | Revoke a connection (soft delete) |
| `GET` | `/data_source_tables?data_source_id=&userId=` | Remote tables of a saved connection + which are registered |
| `POST` | `/data_source_tables_preview` | Remote tables for **unsaved** credentials (Add wizard) |
| `POST` | `/test_data_source` | Verify unsaved credentials |
| `POST` | `/run_data_source` | Run a read-only SELECT in the target's native dialect |

Plan limit is also exposed by the existing billing endpoint:
`GET /billing/me` → `plan.max_data_sources` (`null` = unlimited) and the
`usage` block. `GET /data_sources` returns the same limit/used pair, so the
settings screen does not need a second call.

---

## 4. Endpoint details

### 4.1 `GET /data_sources?userId=<id>`

```json
{
  "data_sources": [
    {
      "id": 7,
      "userId": 42,
      "name": "Production MySQL",
      "dbtype": "mysql",
      "dburl": "db.client.com",
      "dbport": 3306,
      "dbname": "shop",
      "dbuser": "reader",
      "has_password": true,
      "ssl": true,
      "status": "active",
      "last_connected_at": "2026-11-10T09:12:44",
      "last_error": null,
      "created_at": "2026-11-08T18:03:21",
      "table_count": 12
    }
  ],
  "limit": 10,
  "used": 1
}
```

- `limit` is `null` for unlimited plans.
- `status`: `active` | `unreachable` (last execution failed) | `revoked` (deleted).
- `dburl` is the **host** only (a `mysql://host:port/db` input is stored stripped).
- `has_password` is informational; the password itself is never returned.
- `table_count` counts currently registered (non-deleted) tables for that source.

### 4.2 `POST /data_source` — Add

```json
{
  "userId": 42,
  "name": "Production MySQL",
  "dbtype": "mysql",
  "dburl": "db.client.com",
  "dbport": 3306,
  "dbname": "shop",
  "dbuser": "reader",
  "dbpassword": "secret",
  "ssl": true,
  "tables": ["orders", "customers"]
}
```

- `dbport` optional — defaults to 3306 / 1433 / 5432 per engine.
- `dbtype` also accepts aliases (`mariadb`, `sqlserver`, `postgresql`, …);
  responses always use the canonical value.
- `name` optional — defaults to `dbname`.
- `tables` optional — **omit to expose every base table** (fails with `400` if the
  database has more than `max_selectable` tables). Send `[]` to expose none.
- The server tests the connection, introspects the selected tables (metadata for
  the AI planner — no AI call), then stores everything atomically.

Response `200`:

```json
{
  "data_source": { /* DataSource, as in 4.1 */ },
  "tables": [{ "id": 91, "name": "orders" }, { "id": 92, "name": "customers" }],
  "connection": { "server_version": "8.0.36", "latency_ms": 42.7 }
}
```

Errors: `403 DATA_SOURCE_LIMIT`, `400 DATA_SOURCE_CONNECTION_FAILED`,
`400 DATA_SOURCE_INTROSPECTION_FAILED`, `400` validation strings
(`Unsupported dbtype '…'`, `dburl is required.`, `Unknown table(s) in this
database: …`, etc.).

### 4.3 `POST /data_source_tables_preview` — tables before saving

Same body as `/test_data_source` (all credential fields above, **no `name`/`tables`**).

```json
{
  "schema": "public",
  "tables": ["customers", "orders", "products"],
  "max_selectable": 200
}
```

Use this in the Add wizard: credentials → preview tables → user picks →
`POST /data_source` with the selected names. On failure returns
`400 DATA_SOURCE_CONNECTION_FAILED` or a validation string.

### 4.4 `GET /data_source_tables?data_source_id=<id>&userId=<id>` — saved source

```json
{
  "data_source_id": 7,
  "schema": "shop",
  "tables": [
    { "name": "customers", "registered": true },
    { "name": "orders", "registered": true },
    { "name": "audit_log", "registered": false }
  ]
}
```

Errors: `404` (source missing), `502 DATA_SOURCE_UNAVAILABLE` (database offline;
`last_error` is updated server-side).

### 4.5 `POST /update_data_source` — Edit

```json
{
  "userId": 42,
  "data_source_id": 7,
  "name": "Production MySQL",
  "dburl": "new-host.client.com",
  "dbport": 3306,
  "dbname": "shop",
  "dbuser": "reader",
  "dbpassword": "new-secret",
  "ssl": true,
  "tables": ["orders", "customers", "audit_log"]
}
```

Semantics:

- Any field **omitted** is left unchanged. Omit `dbpassword` to keep the stored one.
- `tables` omitted → registered tables are kept as-is. Exception: if the
  connection target changed (`dbtype`, host, port, dbname or user), the backend
  re-introspects the already-registered tables so their metadata stays correct.
  If one of them no longer exists, the update fails with `400 Unknown table(s)…`.
- `tables` provided → becomes the exact exposed set: new ones are registered,
  missing ones are unregistered (their sessions/dashboards are soft-deleted).
- The connection is always tested before anything is written.

Response `200`:

```json
{
  "data_source": { /* DataSource */ },
  "connection": { "server_version": "8.0.36", "latency_ms": 51.2 },
  "tables": [{ "id": 91, "name": "orders" }],
  "removed_tables": [{ "id": 88, "name": "old_table" }]
}
```

`tables` / `removed_tables` are present only when a table sync actually ran
(`tables` sent, or the connection target changed). `removed_tables` is a **list**
here; in the delete response it is a **count** (see 4.6).

### 4.6 `POST /delete_data_source` — Remove

```json
{ "userId": 42, "data_source_id": 7 }
```

Response `200`:

```json
{ "id": 7, "deleted": true, "removed_tables": 12 }
```

- Soft delete: the source is marked `revoked`, its secret is wiped, and its
  registered tables are unregistered.
- Sessions and dashboards built on those tables are soft-deleted; shared
  dashboards are unshared.
- **The client's database is never modified.**

### 4.7 `POST /test_data_source`

Body: `userId`, `dbtype`, `dburl`, `dbport?`, `dbname`, `dbuser`, `dbpassword`, `ssl?`.

```json
{ "server_version": "PostgreSQL 16.2 ...", "latency_ms": 38.1 }
```

Errors: `400 DATA_SOURCE_CONNECTION_FAILED`, `400` validation string.

### 4.8 `POST /run_data_source` — Run DB console

```json
{ "userId": 42, "data_source_id": 7, "sql": "SELECT * FROM orders LIMIT 100", "limit": 100 }
```

- `sql` is executed in the **target database's native dialect** (not T-SQL).
- Only read-only `SELECT` / `WITH` statements; anything else → `400`.
- `limit` cap is `CUSTOM_DB_MAX_ROWS` (default **5000**); omitted = 5000.

Response `200`:

```json
{
  "data_source_id": 7,
  "columns": ["id", "total"],
  "rows": [[1, 99.5], [2, 12.0]],
  "row_count": 2,
  "limit": 100
}
```

Errors: `409` (revoked), `502 DATA_SOURCE_QUERY_FAILED`, `400` unsafe SQL.

---

## 5. Error model

FastAPI wraps errors as `{"detail": ...}`. `detail` is either a **string** or an
object with a `code`.

| Status | `detail.code` | Meaning / UI action |
| --- | --- | --- |
| 400 | *(string)* | Validation — show inline under the field. |
| 400 | `DATA_SOURCE_CONNECTION_FAILED` | Bad credentials / host unreachable / TLS failure. Show message + retry. |
| 400 | `DATA_SOURCE_INTROSPECTION_FAILED` | Connected, but schema could not be read (permissions). |
| 401 | *(string)* | Token invalid or `userId` mismatch — sign in again. |
| 403 | `DATA_SOURCE_LIMIT` | `{ limit, used }` — show upsell, disable "Add database". |
| 404 | *(string)* | Source/table gone — refresh the list. |
| 409 | *(string)* | Connection revoked while a session/table was in use. |
| 502 | `DATA_SOURCE_UNAVAILABLE` | Database offline when listing tables. |
| 502 | `DATA_SOURCE_QUERY_FAILED` | Execution failed; may include `data_source_id`. Retry later; surface `last_error`. |
| 500 | *(string)* | Unexpected — generic message. |

Example:

```json
{ "detail": { "code": "DATA_SOURCE_LIMIT", "message": "Your Individual plan allows 10 database connection(s).", "limit": 10, "used": 10 } }
```

---

## 6. TypeScript types

```ts
export type DbType = "mysql" | "mssql" | "postgres";
export type DataSourceStatus = "active" | "unreachable" | "revoked";

export interface DataSource {
  id: number;
  userId: number;
  name: string;
  dbtype: DbType;
  dburl: string;            // host only
  dbport: number;
  dbname: string;
  dbuser: string;
  has_password: boolean;
  ssl: boolean;
  status: DataSourceStatus;
  last_connected_at: string | null;
  last_error: string | null;
  created_at: string | null;
  table_count: number;
}

export interface DataSourceListResponse {
  data_sources: DataSource[];
  limit: number | null;     // null = unlimited
  used: number;
}

export interface ConnectionInfo {
  server_version: string;
  latency_ms: number;
}

export interface RegisteredTable {
  id: number;
  name: string;
}

export type CreateDataSourceRequest = {
  userId: number;
  name?: string;
  dbtype: DbType | string;
  dburl: string;
  dbport?: number;
  dbname: string;
  dbuser: string;
  dbpassword: string;
  ssl?: boolean;
  tables?: string[];        // omit = all base tables
};

export type UpdateDataSourceRequest = {
  userId: number;
  data_source_id: number;
  name?: string;
  dbtype?: DbType | string;
  dburl?: string;
  dbport?: number;
  dbname?: string;
  dbuser?: string;
  dbpassword?: string;      // omit = keep stored
  ssl?: boolean;
  tables?: string[];        // omit = unchanged
};

export interface CreateDataSourceResponse {
  data_source: DataSource;
  tables: RegisteredTable[];
  connection: ConnectionInfo;
}

export interface UpdateDataSourceResponse {
  data_source: DataSource;
  connection: ConnectionInfo;
  tables?: RegisteredTable[];
  removed_tables?: RegisteredTable[];
}

export interface DeleteDataSourceResponse {
  id: number;
  deleted: true;
  removed_tables: number;   // count here, list on update
}

export interface RemoteTableListResponse {
  data_source_id: number;
  schema: string | null;
  tables: { name: string; registered: boolean }[];
}

export interface TablePreviewResponse {
  schema: string | null;
  tables: string[];
  max_selectable: number;
}

export interface TestConnectionResponse extends ConnectionInfo {}

export interface RunDataSourceResponse {
  data_source_id: number;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  limit: number;
}

export interface ApiErrorDetail {
  code: string;
  message: string;
  limit?: number;
  used?: number;
  data_source_id?: number;
}
```

---

## 7. UI flows

### 7.1 Add a database (wizard)

1. **Engine** — three cards: MySQL / SQL Server / Postgres.
2. **Connection form** — host (`dburl`), port (prefilled default), database name,
   user, password, SSL toggle, optional display name.
3. **Test** (optional) — `POST /test_data_source`; show server version + latency.
4. **Tables** — `POST /data_source_tables_preview`; multi-select with search,
   select all/none, enforce `max_selectable`. (Skipping this step = expose all.)
5. **Save** — `POST /data_source` with `tables`; show registered count and
   connection info.
6. On `403 DATA_SOURCE_LIMIT`, show the plan limit and an upgrade CTA.

### 7.2 Manage connections

- List via `GET /data_sources`; badge `status`, show `engine • host:port/db`,
  `table_count`, humanized `last_connected_at`.
- `unreachable` → tooltip with `last_error`; still editable/runnable (it may recover).
- Entry points per row: **Edit**, **Delete**, **Run**, **Open tables**.

### 7.3 Edit

- Prefill from the `DataSource` object; leave password blank (placeholder:
  "unchanged").
- If the user edits tables, load current state from
  `GET /data_source_tables` (checkbox list, `registered` = checked) and submit
  the desired set as `tables`.
- Warn that unchecking a table removes its sessions and dashboards.
- If host/database changed without touching tables, the backend re-syncs
  metadata automatically; a `400 Unknown table(s)…` means the new database does
  not contain a previously exposed table — prompt the user to re-select tables
  (send `tables` explicitly).

### 7.4 Delete

- Confirmation copy must state: **remote data is untouched**; local datasets,
  sessions and dashboards for its tables are removed.
- On `200`, remove the row and refresh the dataset list.

### 7.5 Run console

- SQL textarea, note "executes in your database's own SQL dialect".
- `POST /run_data_source`; render `columns`/`rows`; show `row_count` and the
  effective `limit` (server may cap at 5000).
- Only `SELECT`/`WITH` allowed; show the server's message for `400`.

### 7.6 Analyze a remote table

No new screens needed:

1. `GET /user_datasets?userId=` now includes remote tables (same shape).
2. Select one → `POST /create_session_for_table?userId=&tableId=` (unchanged).
3. `POST /transform`, dashboards, `GET /table_preview`, `GET /view_original`
   all work unchanged; execution is routed to the owning database server-side.

---

## 8. Behaviour the frontend must know

- **Cross-database merges are blocked.** `POST /merge_session` with sessions from
  different databases returns `400`. `POST /merge_upload` into a custom-DB
  session returns `400` before ingesting. Only merge sessions that share the same
  source (or are both uploaded datasets).
- **Dialect transparency.** Generated SQL is stored as T-SQL and translated at
  execution for MySQL/Postgres. Error codes surface as the usual
  `SQL_SYNTAX`, `SQL_INVALID_COLUMN`, `SQL_GROUP_BY`, `SQL_CONVERSION`,
  `SQL_DIVIDE_BY_ZERO`, `SQL_TIMEOUT` (plus `UNKNOWN`).
- **Revoked sources.** If a connection is deleted while a screen is open,
  transform/preview return `409` and in-flight dashboard widgets fail without
  breaking the page; refresh the dashboard.
- **Previews are capped.** Remote previews/`view_original` fetch at most
  `CUSTOM_DB_MAX_ROWS` (5000) rows; `total_count` still shows the exact count.
- **One default schema per source.** Table names are unqualified; if the client
  needs another schema, that is a future backend change.
- **Plan limits come from the server**, never hard-code them:
  `GET /billing/me` → `plan.max_data_sources` (Free 2, Individual 10, Maxx 25,
  Enterprise `null` in the default seed).

---

## 9. Quick cURL reference

```bash
# list
curl -H "Authorization: Bearer $TOKEN" \
  "$API/data_sources?userId=42"

# test unsaved credentials
curl -X POST "$API/test_data_source" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"userId":42,"dbtype":"postgres","dburl":"db.client.com","dbport":5432,
       "dbname":"shop","dbuser":"reader","dbpassword":"secret","ssl":true}'

# tables before saving
curl -X POST "$API/data_source_tables_preview" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{ ...same body as above... }'

# add
curl -X POST "$API/data_source" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"userId":42,"dbtype":"mysql","dburl":"db.client.com","dbname":"shop",
       "dbuser":"reader","dbpassword":"secret","tables":["orders","customers"]}'

# run
curl -X POST "$API/run_data_source" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"userId":42,"data_source_id":7,"sql":"SELECT * FROM orders LIMIT 50","limit":50}'
```

---

## 10. Backend files (for reference)

| Area | File |
| --- | --- |
| Routes | `api/routers/data_sources.py` |
| CRUD / gating / run | `api/services/custom_db/service.py` |
| Execution routing | `api/services/custom_db/execution.py` |
| Dialect translation | `api/services/custom_db/dialects.py` |
| Drivers / timeouts | `api/services/custom_db/connectors.py` |
| Table introspection | `api/services/custom_db/introspection.py` |
| Password encryption | `api/core/crypto.py` |
| Schema / plan limits | `sql/init-scripts/init.sql` |
