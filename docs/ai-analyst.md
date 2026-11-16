# AI Analyst

AI Analyst turns one uploaded table into a five-step trend analysis and a
dashboard-style report with an AI-written executive summary. The report can be
saved, shared with a read-only link, and exported as a PDF (including charts).

Because a run is expensive (two LLM calls plus one query per step), the result
is persisted in `analyst_reports` — one report per user and table. Opening or
sharing a saved report never calls the AI; only an explicit **Re-run** does.

---

## 1. API

All endpoints are under the FastAPI app mounted by `api/main.py` and require
the normal `Authorization: Bearer <access token>` header.

### `POST /ai_analyst`

Runs the analysis, or returns the saved report for the table. The only required
input is the id of a table returned by `POST /upload`.

Request:

```json
{ "table_id": 42, "force": false }
```

- `force: false` (default) — if this user already has a saved report for the
  table, it is returned immediately and **no AI is used**.
- `force: true` — always runs a fresh analysis (used by the page's Re-run
  button after confirmation). The saved report is replaced.

Response (abridged):

```json
{
  "report_id": 7,
  "reused": false,
  "table_id": 42,
  "table_name": "orders_9f2c",
  "analysis_title": "Orders — Trend Analysis",
  "analysis_goal": "Explain the revenue trend and its main drivers.",
  "target_audience": "Business stakeholders",
  "narrative_summary": "Revenue rose 18% between January and June, driven mainly by ...",
  "steps": [
    {
      "id": "step_1",
      "title": "Overall revenue trend",
      "question": "How does revenue move over order date?",
      "chart_type": "line",
      "x_column": "order_date",
      "y_column": "revenue",
      "aggregation": "sum",
      "status": "ok",
      "error": null,
      "insight": "Revenue grew from 1.2M to 1.4M between January and June."
    }
  ],
  "spec": {
    "dashboard_title": "Orders — Trend Analysis",
    "dashboard_goal": "Explain the revenue trend and its main drivers.",
    "dashboard_type": "analytical",
    "target_audience": "Business stakeholders",
    "kpis": [ { "id": "kpi_1", "title": "Revenue", "format": "currency", "...": "..." } ],
    "charts": [ { "id": "step_1", "chart_type": "line", "x_column": "order_date", "y_column": "revenue" } ]
  },
  "data": {
    "step_1": { "columns": ["order_date", "revenue"], "rows": [["2025-01-01", 1200000]] },
    "kpi_1":  { "columns": ["value"], "rows": [[4150000]] }
  },
  "trace_id": "1f3a9c22b7de"
}
```

Notes:

- `steps` always contains five entries. Each one is a real SQL aggregate that
  ran against the table; `status` is `ok` or `failed` and `error` carries a
  short code (`SQL_NOT_GENERATED`, `SQL_CONVERSION`, …).
- `spec` mirrors the dashboard contract, so the frontend can reuse the same
  KPI/chart rendering.
- `data` is keyed by widget id and is shaped like `WidgetData`
  (`{columns, rows}`).
- `trace_id` also appears in backend logs as `AI_TRACE {...}` for every stage.
- `report_id`, `reused`, `is_shared`, `share_token`, `shared_at`,
  `snapshot_stale` and `updated_at` describe the saved record
  (`snapshot_stale` means the report was re-run after the share link was
  created — refresh the link to publish the new numbers).

### Saved reports (no AI)

| Endpoint | Purpose |
|---|---|
| `GET /analyst_report?report_id=&userId=` | Load one saved report (same payload as above). |
| `GET /user_analyst_reports?userId=[&table_id=]` | List saved reports, newest first. |
| `POST /delete_analyst_report` | Soft-delete a report and disable its share link. |
| `POST /share_analyst_report` | Enable/disable/refresh sharing. |
| `GET /shared_analyst_report?token=` | Public read-only snapshot (no auth, rate limited). |

`POST /share_analyst_report` body:

```json
{ "userId": 1, "report_id": 7, "enabled": true, "refresh": false }
```

The snapshot is built server-side from the stored payload and strips SQL and
failed sections. Share tokens are `secrets.token_urlsafe(24)` and validated
with `^[A-Za-z0-9_-]{16,64}$` on the public endpoint, which also returns
`Cache-Control: no-store`.

### `POST /ai_analyst/export_pdf`

Renders an already-computed report as a PDF. The client posts back the
narrative, step metadata and result rows it received from `/ai_analyst`; the
server does **not** call an LLM here, so exporting cannot fail because of an AI
outage. Each successful step renders its chart (bar, column, line, pie or
scatter) above the result table; an unsupported chart never fails the export.

Request body: the same `analysis_title`, `analysis_goal`, `table_name`,
`narrative_summary`, `kpis`, `steps`, and `data` objects as above.

Response: `application/pdf` with `Content-Disposition: attachment`.

The endpoint caps the payload (10 KPIs, 10 steps, 500 rows) so a malformed
request cannot exhaust memory.

### Storage

Reports live in `analyst_reports` (`sql/init-scripts/init.sql`), one row per
user and table, with the rendered `payload` JSON plus share columns. The table
is created by the init script every time the SQL Server container starts
(guarded by `IF NOT EXISTS`), so an existing volume picks it up on the next
restart. Deleting a dataset or an account soft-deletes its reports and
disables their share links.

---

## 2. How the analysis is produced

Implementation: `api/services/analyst.py` (orchestration and prompts),
`api/services/analyst_pdf.py` (PDF layout), `api/routers/analyst.py` (HTTP,
auth, metering).

1. **Design** — `design_analysis()` sends the table's metadata and knowledge
   base to the LLM and asks for a title, three KPIs and exactly five analysis
   steps in a fixed narrative order:
   overall trend → drivers → top performers → composition → comparison/outliers.
2. **Validate** — every returned column is checked against the real metadata.
   Unknown columns are replaced from a deterministic fallback plan; missing
   steps are filled from the same plan; invalid chart types/aggregations are
   normalised. The fallback plan is built purely from column roles, so the
   endpoint still works when the LLM returns garbage or is unavailable.
3. **SQL + data** — the validated plan is handed to the existing dashboard
   engine (`services/dashboard.build_dashboard`). That engine writes SQL,
   checks it is a safe `SELECT`, runs it through the table's execution source
   (internal SQL Server or a custom client DB), retries once with an LLM
   repair, and records per-widget `status`/`error`.
4. **Narrative** — `build_narrative()` sends only the *actual returned rows*
   (max 8 rows per widget) and asks for a short executive summary plus one
   takeaway per step. The prompt forbids inventing numbers. If the call or
   parsing fails, a deterministic summary is generated from the same rows, so
   the user always gets a report.
5. **Response, then save** — if every step and KPI failed, the endpoint returns
   a 422 (`ANALYSIS_NO_DATA`) instead of an empty report. A successful run is
   upserted into `analyst_reports` (one row per user + table); a failed save is
   logged but never discards the freshly computed report.

---

## 3. Hallucination guards

- Column references are validated against `table_info.metadata`; invented
  columns never reach SQL generation.
- SQL is generated by the same constrained prompt used by dashboards
  (single table, bracketed names, aliased output columns, `SELECT` only) and
  checked with `core.sql_safety.is_safe_select`.
- Narrative numbers come from executed rows, not from the LLM, and a
  deterministic fallback is used if the narrative call fails.
- Per-step failures are reported individually (`status: "failed"`), so a
  partial report is shown honestly rather than silently omitted.

---

## 4. Billing and limits

- Loading, listing, sharing and PDF export never call the AI and are never
  blocked by usage limits.
- `usage_service.check_quota()` runs only for a first run or a forced re-run,
  before any AI work.
- All LLM calls happen inside `usage_service.metered_call(user_id, "ai_analyst", trace_id)`,
  so tokens are recorded even if the analysis fails mid-way.
- A successful run counts toward the plan's monthly query cap only when at
  least one LLM call was made.

---

## 5. Error envelope

All failures use the standard envelope:

```json
{
  "detail": {
    "code": "ANALYST_FAILED",
    "message": "The analysis couldn't be completed. Please try again.",
    "retryable": true,
    "trace_id": "1f3a9c22b7de"
  }
}
```

| Code | HTTP | Meaning |
|---|---|---|
| `TABLE_NOT_FOUND` | 404 | Table id is missing or soft-deleted |
| `REPORT_NOT_FOUND` | 404 | Saved report is missing or soft-deleted |
| `FORBIDDEN` | 403 | Table or report belongs to another user |
| `TABLE_NOT_READY` | 422 | Table has no column metadata yet |
| `ANALYSIS_NO_DATA` | 422 | No step or KPI could be computed |
| `NOTHING_TO_SHARE` | 422 | All report sections failed; nothing to snapshot |
| `ANALYST_FAILED` | 500 | Unexpected failure (logged with trace id) |
| `SHARE_FAILED` | 500 | Sharing update failed |
| `PDF_FAILED` | 500 | PDF layout failed |
| `USAGE_LIMIT` | 429 | Plan quota reached (shared envelope) |

---

## 6. Frontend

- Entry point: the **AI Analyst** chip in the transformation workspace toolbar
  (`genbi/lib/components/workspace_toolbar.dart`).
- If the table has no saved report yet, clicking it opens the confirmation
  dialog (`components/ai_analyst_dialog.dart`), which warns that the run uses
  AI quota. If a saved report exists, the report opens immediately with no
  dialog and no AI call.
- `pages/ai_analyst_page.dart` shows the report: header, narrative card, KPI
  row, and the five step cards (chart + takeaway). Failed steps render an
  inline explanation instead of a chart.
- The app bar exposes **Share** (same toggle/copy-link dialog as dashboards,
  using `/#/share_analyst/<token>`), **Export PDF** and **Re-run**. Re-run asks
  for confirmation first because it spends AI quota.
- Saved reports are listed in the sidebar under **AI REPORTS**
  (`components/sidebar_analyst_reports.dart`), so they can be reopened from
  any dataset workspace.
- `ApiService` implements `runAiAnalyst(tableId:, force:)`,
  `getAnalystReport`, `userAnalystReports`, `shareAnalystReport`,
  `deleteAnalystReport`, `exportAnalystPdf` and `getSharedAnalystReport`.
- The analysis always runs on the **uploaded table** (the `tableId` assigned
  at upload), not on an intermediate transformation step.

---

## 7. Manual verification

1. Upload a CSV/XLSX and wait for the workspace to load.
2. Click **AI Analyst** in the toolbar and confirm the dialog.
3. Check that five steps appear, each with a chart and a takeaway, and that
   the KPI values match the data.
4. Close and click **AI Analyst** again: the saved report must open instantly
   without the confirmation dialog (check the logs show `analyst.reused` and no
   LLM call).
5. Click **Export PDF** and confirm the downloaded file contains the narrative,
   KPIs, a chart for every successful step and the step tables.
6. Click **Share**, copy the link and open it in a private window — the report
   must render read-only and the URL must be `/#/share_analyst/<token>`.
7. Click **Re-run**, confirm, and check that the report updates and any share
   link now shows the stale-snapshot banner until refreshed.
8. To test degraded paths, temporarily set an invalid `GROQ_API_KEY` /
   `OPENROUTER_API_KEY`: the plan falls back to deterministic steps and the
   narrative falls back to numbers computed from the rows; the page still
   renders.
