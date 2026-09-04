# NBA Console — React + FastAPI

A React (Vite + TypeScript + Tailwind) front end over a thin FastAPI backend that
**reuses the existing Streamlit app's Python logic unchanged** (scoring, Lakebase
dual-branch access, Genie, Foundation-Model drafting). One process: FastAPI serves
the compiled SPA (`frontend/dist`) at `/` and JSON at `/api/*`.

This lives **alongside** the live Streamlit app in `../app` (which is untouched).
It is not wired into the bundle yet — deploy it later as a **new app** using the
copy-paste snippets in [§3](#3-deploy-later-as-a-new-app-additive-only).

```
src/app_react/
  app.yaml                     # command: uvicorn backend.main:app --host 0.0.0.0 --port 8000
  requirements.txt             # backend deps (installed by Databricks Apps at deploy)
  backend/
    main.py                    # FastAPI app: /api router + StaticFiles(frontend/dist)
    api.py                     # all /api/* routes (thin wrappers)
    config.py                  # env-var reads (SAME names as the Streamlit app)
    db.py                      # host resolution + psycopg2 conns (prod + app-writes) + CRUD + decisions
    nba_core.py                # score_actions, orchestration, explain, band, trajectory
    llm.py                     # call_llm + draft_outreach
    genie.py                   # genie_ask + query-result fetch (returns {columns, rows})
    requirements.txt           # (local convenience copy of ../requirements.txt)
  frontend/
    src/
      pages/     Lookup  Decisions  Actions  ChangeLog  AskNba
      components/ AppShell KpiCard ActionCard AssistDrawer ScoreBar SeverityPill
                  DataTable ChatPanel GenieLauncher Drawer ConfigFooter Skeleton ui
      lib/        api.ts (typed fetch) store.ts (Genie state) format.ts
    index.html  vite.config.ts  tailwind.config.ts  tsconfig*.json
```

Nothing about the workspace is baked into the JS bundle. The frontend fetches
`/api/config` at runtime; the backend reads the same env vars as the Streamlit app.

---

## 1. Run locally (against the `genie` target)

You need the Databricks CLI/SDK authenticated for the `genie` workspace so the app
SP path (`Config().authenticate()`, `w.postgres.get_endpoint`, the Lakebase
credentials API, Model Serving, and Genie) works exactly as it does in production.

### 1a. Auth + config env vars

```bash
# Point the SDK at the genie workspace (use your CLI profile for that host,
# https://fevm-humana-nba-genie.cloud.databricks.com).
export DATABRICKS_CONFIG_PROFILE=<your-genie-profile>

# Same env-var contract as the Streamlit app. Values are the databricks.yml
# `genie` target variables.
export LAKEBASE_PROJECT=nba-lakebase-genie
export LAKEBASE_BRANCH_PRODUCTION=production
export LAKEBASE_BRANCH_APP_WRITES=app-writes
export LAKEBASE_DATABASE=databricks_postgres
export LAKEBASE_SCHEMA=nba_new_lbase
export MODEL_ENDPOINT_NAME=nba-scoring-endpoint
export GENIE_SPACE_ID=01f1a2271a4618a8be35471ddcfb87f6
export LLM_ENDPOINT_NAME=databricks-claude-haiku-4-5
```

> Local Lakebase reads require your identity (or the profile's SP) to have
> `CAN_CONNECT` on the `nba-lakebase-genie` branches. If you can't connect
> locally, the UI still renders — endpoints that need Lakebase return empty
> lists / errors, and Genie + `/api/config` still work.

### 1b. Backend

```bash
cd src/app_react
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### 1c. Frontend

**Option A — one process (what production does):** build the SPA once, then just
run the backend (it serves `frontend/dist`):

```bash
cd src/app_react/frontend
npm install
npm run build          # → frontend/dist
# now open http://localhost:8000  (uvicorn from 1b serves the built SPA)
```

**Option B — hot-reload dev loop:** run Vite and uvicorn side by side. Vite
proxies `/api` → `http://localhost:8000` (see `vite.config.ts`):

```bash
# terminal 1
cd src/app_react && .venv/bin/uvicorn backend.main:app --reload --port 8000
# terminal 2
cd src/app_react/frontend && npm run dev   # open http://localhost:5173
```

---

## 2. Build

```bash
cd src/app_react/frontend
npm install
npm run build     # tsc -b && vite build  →  frontend/dist/{index.html,assets/*}
```

`backend/main.py` resolves `frontend/dist` relative to itself and serves it via
`StaticFiles` (assets under `/assets`, SPA fallback for client routes). **Commit
`frontend/dist`** (or build it in CI before deploy) so the app runtime needs no
Node — the `.gitignore` here deliberately does *not* ignore `dist/`.

Verify the backend imports and serves cleanly:

```bash
cd src/app_react
.venv/bin/python -c "import backend.main; print('ok')"
```

---

## 3. Deploy later as a NEW app (additive only)

> These snippets are **ready to paste** but are intentionally **not applied** —
> `databricks.yml` and `resources/` were left untouched so the live Streamlit
> `nba-console-genie` app is never at risk. Apply them when you're ready to run
> the React app in parallel, then deploy/cut over.

### 3a. New bundle variable (`databricks.yml` → `variables:`)

```yaml
  app_name_react:
    description: Databricks App name for the React + FastAPI NBA console.
    default: nba-console-react
```

### 3b. Set it on the `genie` target (`databricks.yml` → `targets: genie: variables:`)

```yaml
      app_name_react: nba-console-genie-react
```

(Every other value the React app needs — `lakebase_*`, `model_endpoint_name`,
`genie_space_id`, `llm_endpoint_name` — is already defined on the `genie` target
and is reused as-is.)

### 3c. New resource file: `resources/nba_app_react.app.yml`

Mirror of `resources/nba_app.app.yml` with a new app key + name and the
`uvicorn` command / `../src/app_react` source path. Same env wiring, same
Lakebase + serving-endpoint bindings.

```yaml
# =============================================================================
# Databricks App — nba-console-react (React + FastAPI)
# Coexists with the Streamlit `nba_console` app; same env-var contract.
# Start after deploy:  databricks bundle run nba_console_react -t genie
# =============================================================================
resources:
  apps:
    nba_console_react:
      name: ${var.app_name_react}
      description: "NBA console (React + FastAPI) — Lakebase + Model Serving + Genie."
      source_code_path: ../src/app_react

      config:
        command:
          - uvicorn
          - backend.main:app
          - --host
          - "0.0.0.0"
          - --port
          - "8000"
        env:
          - name: LAKEBASE_PROJECT
            value: ${var.lakebase_project}
          - name: LAKEBASE_BRANCH_PRODUCTION
            value: ${var.lakebase_branch}
          - name: LAKEBASE_BRANCH_APP_WRITES
            value: ${var.app_writes_branch}
          - name: LAKEBASE_DATABASE
            value: ${var.lakebase_database}
          - name: LAKEBASE_SCHEMA
            value: ${var.lakebase_schema}
          - name: MODEL_ENDPOINT_NAME
            value: ${var.model_endpoint_name}
          - name: GENIE_SPACE_ID
            value: ${var.genie_space_id}
          - name: LLM_ENDPOINT_NAME
            value: ${var.llm_endpoint_name}

      resources:
        - name: lakebase-production
          description: "Read path — member_features (production branch)."
          postgres:
            branch: projects/${var.lakebase_project}/branches/${var.lakebase_branch}
            database: projects/${var.lakebase_project}/branches/${var.lakebase_branch}/databases/${var.lakebase_database_id}
            permission: CAN_CONNECT_AND_CREATE
        - name: lakebase-app-writes
          description: "CRUD path — action_catalog + audit (app-writes branch)."
          postgres:
            branch: projects/${var.lakebase_project}/branches/${var.app_writes_branch}
            database: projects/${var.lakebase_project}/branches/${var.app_writes_branch}/databases/${var.lakebase_database_id}
            permission: CAN_CONNECT_AND_CREATE
        - name: nba-model
          description: "Scoring endpoint."
          serving_endpoint:
            name: ${var.model_endpoint_name}
            permission: CAN_QUERY
```

### 3d. Deploy + grants + start

```bash
cd src/app_react/frontend && npm run build && cd ../../..   # ensure dist/ is fresh
databricks bundle validate -t genie
databricks bundle deploy   -t genie                          # creates the new app too

# Grant the NEW app SP everything Genie needs (space CAN_RUN, warehouse CAN_USE,
# UC SELECT). scripts/grant_genie_access resolves the SP by app name — point it
# at the new app_name_react (nba-console-genie-react).
databricks bundle run nba_console_react -t genie             # start it
```

The existing `nba-console-genie` (Streamlit) keeps running throughout; retire it
only after React parity is confirmed.

---

## Notes / risks / TODO

- **Auth is unchanged.** All Databricks calls reuse the copied logic verbatim:
  `Config().authenticate()` for Model Serving + Genie + the Lakebase credentials
  API, and `w.postgres.get_endpoint` for host resolution (self-heals across
  branch re-forks). No new auth code.
- **Serialization:** the Genie SQL result crosses the API boundary as
  `{columns: string[], rows: any[][]}` (the Streamlit version used a DataFrame);
  `get_change_log` returns JSON records. `datetime`/`Decimal` are handled by
  FastAPI's `jsonable_encoder`.
- **`/api/members/{id}/score`** returns each ranked action already enriched with
  `explain`, `band`, and `trajectory`, so the Assist drawer needs no second call.
- **Cold starts** (model ~60s, warehouse ~30s) are unchanged; the UI uses skeleton
  loaders + TanStack Query retries instead of a blocking spinner.
- **Local Lakebase access** needs your identity/profile SP to have `CAN_CONNECT`
  on the branches; without it, DB-backed endpoints return empty/errors but the app
  and Genie still run.
- **TODO (deploy):** applying §3 and pointing the Genie grant helper at
  `app_name_react` is the only remaining step; not done here per instructions.
