# NBA Prototype — Next Best Action on Databricks

A real-time **Next Best Action** engine for a healthcare payer, built entirely on
Databricks. It combines the Lakehouse (Unity Catalog + ML Model Serving) with
**Lakebase** Postgres for sub-5ms operational reads and branch-based CRUD
isolation, and ships as a **Databricks Asset Bundle** so it deploys to any
workspace by changing configuration — never code.

> Packaged for GitHub + Databricks Asset Bundles. Every workspace-, catalog-,
> Lakebase-, and endpoint-specific value is a **bundle variable** passed to
> notebooks (as widgets) and to the app (as env vars). Nothing is hardcoded.

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                    Unity Catalog (SOURCE OF TRUTH)                          │
│   <catalog>.<schema>.serving_member_features                                │
│   <catalog>.<schema>.action_catalog        (governed source of truth)       │
│   <cdf_catalog>.<cdf_schema>.lb_action_catalog_history  (CDF change feed)    │
└──────────────────┬──────────────────────────────────┬──────────────────────┘
                   │ sync_nba_to_lakebase             ▲ reconcile_action_catalog
                   │ (member_features, full replace)  │ (reads CDF by LSN,
                   ▼                                   │  MERGEs net changes to UC)
┌────────────────────────────────┐     ┌──────────────────────────────────────┐
│ PRODUCTION branch (Lakebase)   │     │ APP-WRITES branch (Lakebase, permanent)│
│ • member_features (read path)  │     │ • action_catalog (CRUD via app)      │
│ • action_catalog               │◄────│ • Lakebase CDF enabled ─────────────▶│──▶ UC history
│ • read-only for the app        │fork │ • full read/write for the app        │
└────────────────┬───────────────┘ once└──────────────────────────────────────┘
                 │
     ┌───────────┴───────────┐
     ▼                       ▼
 Model Serving Endpoint   Databricks App (nba-console)
 (nba-scoring-endpoint)   🎯 Member Lookup → production
 LightGBM, scale-to-zero  ⚙️  Manage Actions → app-writes
                          📜 Change Log → app-writes (legacy audit, testing only)
```

## Repository structure

```
nba_prototype/
├── databricks.yml.template        # Bundle config template — copy to databricks.yml
│                                   #   (real databricks.yml is gitignored)
├── resources/
│   ├── nba_lakebase.yml           # Lakebase project (postgres_projects) — created by the bundle
│   ├── nba_jobs.yml               # Jobs: seed, train, bootstrap, daily sync, reconcile, reset-catalog
│   └── nba_app.app.yml            # Databricks App (nba-console) resource
├── src/
│   ├── app/
│   │   ├── app.py                 # Streamlit NBA console (3 pages)
│   │   ├── app.yaml               # App runtime config (env-driven defaults)
│   │   └── requirements.txt
│   └── notebooks/
│       ├── seed_uc_source_tables.py        # Create UC source tables (synthetic) — run first
│       ├── sync_nba_to_lakebase.py         # UC → Lakebase production sync
│       ├── reconcile_action_catalog.py     # Lakebase CDF → UC → production (by LSN)
│       └── nba_model_training_and_deploy.py   # Train + deploy scoring endpoint
├── scripts/                       # One-command helpers (config-driven, any workspace)
│   ├── setup.sh                   #   full install (deploy→seed→train→bootstrap→app)
│   ├── demo.sh                    #   reconcile demo: edit → CDF → reconcile → verify
│   ├── reset.sh                   #   quick clean slate (baseline; --full for rebuild)
│   ├── destroy.sh                 #   full teardown (app + jobs + purge project + drop UC schemas)
│   └── README.md                  #   how to use the scripts
├── design/
│   └── CDF_reconciliation_design.md   # CDF design + net-change/watermark internals
├── images/                        # Architecture diagrams
├── .env.example                   # Documented configuration knobs
├── README.md
├── SETUP.md                       # Detailed deployment guide
├── LICENSE
└── CONTRIBUTING.md
```

## Prerequisites

| Component | Requirement |
| --- | --- |
| Databricks CLI | v0.287.0+ (for `postgres_*` bundle resources; v0.239.0 min for Apps) |
| Databricks Workspace | Unity Catalog enabled; Databricks Apps + Lakebase enabled |
| Databricks SDK | `>= 0.118.0` (for `w.postgres.*` API) |
| Lakebase project | **Created by the bundle** (`resources/nba_lakebase.yml`) — no manual step |
| UC source tables | Auto-created by `nba_seed_data` (synthetic) — or bring your own |

## Fastest path — 2 setup steps, then 1 command

**Do these 2 one-time things first:**

1. **Install + log in to the Databricks CLI** (v0.287.0+):
   ```bash
   databricks auth login --host https://<your-workspace> --profile <profile>
   export DATABRICKS_CONFIG_PROFILE=<profile>
   ```
2. **Point the bundle at your workspace:** copy the template and edit host + names:
   ```bash
   cp databricks.yml.template databricks.yml   # then set targets.<name>.workspace.host + variables
   ```

> **No manual Lakebase project step.** The bundle creates the Lakebase project
> (and its `production` branch) as a `postgres_projects` resource
> (`resources/nba_lakebase.yml`). Requires CLI v0.287.0+.

**Then install everything with one command** ([`scripts/`](scripts/README.md),
config-driven, works in any workspace):

```bash
./scripts/setup.sh <target>    # full install: Lakebase project + data + model + app + CDF — zero manual steps (~15-20 min)
```

That's it — the app is live. Day-to-day after that:

```bash
./scripts/demo.sh  <target>    # narrated demo: edit -> CDF -> reconcile -> live on production
./scripts/reset.sh <target>    # quick clean slate before a demo
```

> Prefer to see each step? The **Quick start** below is the exact manual sequence
> those scripts run — useful for understanding, debugging, or CI. You don't need
> it if `setup.sh` worked.

## Everyday commands (cheat sheet)

| Task | Command |
| --- | --- |
| Publish an action edit (after editing in the app) | `databricks bundle run nba_reconcile -t <target>` |
| Refresh member features (daily sim) | `databricks bundle run nba_daily_sync -t <target>` |
| Reset actions to the 16 baseline | `databricks bundle run nba_reset_action_catalog -t <target>` |
| Quick clean slate before a demo | `./scripts/reset.sh <target>` |
| Run the narrated demo | `./scripts/demo.sh <target>` |
| Start / restart the app | `databricks bundle run nba_console -t <target>` |
| Full clean rebuild (destructive) | `./scripts/reset.sh <target> --full` |
| Tear down everything (app + jobs + Lakebase project + UC schemas) | `./scripts/destroy.sh <target>` |

## Quick start — from zero in a brand-new workspace

This is the exact, **tested** order to install into an empty workspace.
[SETUP.md](SETUP.md) explains each step. The only manual step the bundle can't do
for you is pointing it at your workspace (step 4) — it creates the Lakebase
project itself.

> **Tooling — you only need the Databricks CLI.** This project has **no
> dependency on Claude, AI tools, or any editor**. Every step below is a plain
> `databricks` CLI command. The `CLAUDE.md` file in the repo is *optional*
> context for developers who happen to use Claude Code; if you don't, ignore it —
> nothing here uses it.

> **Why the app is deployed in two passes.** The `nba-console` app binds to the
> Lakebase `app-writes` branch and the model serving endpoint. Neither exists on
> a first deploy, so the *initial* `bundle deploy` creates the **jobs only** and
> the app resource errors (harmless). You run the pipeline jobs to create those
> dependencies, then deploy again — the app is created on the second pass. This
> is a one-time bootstrap ordering; steady-state deploys are a single command.

```bash
# 1. Clone the repo
git clone <repo-url>
cd nba_prototype

# 2. Install the Databricks CLI (v0.239.0+) — the ONLY required tool
brew tap databricks/tap && brew install databricks   # macOS; see docs for other OSes
databricks --version

# 3. Authenticate (opens a browser for OAuth)
databricks auth login --host https://<your-workspace> --profile <profile>
export DATABRICKS_CONFIG_PROFILE=<profile>

# 4. Create your bundle config from the template, then point it at this
#    workspace: set targets.<name>.workspace.host + variables (uc_catalog,
#    lakebase_project, app_name, ...). The real databricks.yml is gitignored.
#    The bundle creates the Lakebase project itself — no manual project step.
cp databricks.yml.template databricks.yml
#    …edit databricks.yml…

# 5. Validate, then deploy. First deploy creates the jobs + the Lakebase project;
#    the app step errors because its Lakebase/endpoint dependencies don't exist
#    yet — that's expected.
databricks bundle validate -t <target>
databricks bundle deploy   -t <target>   # jobs + Lakebase project created; app deferred (expected error)

# 6. Seed synthetic UC source tables (member_features + action_catalog)
databricks bundle run nba_seed_data   -t <target>

# 7. Train + deploy the scoring model endpoint (~10 min)
databricks bundle run nba_train_model -t <target>

# 8. Bootstrap Lakebase: sync data + fork the permanent app-writes branch +
#    set REPLICA IDENTITY FULL + create the CDF schema/watermark. (SP grants
#    are skipped here — the app doesn't exist yet. Expected.)
databricks bundle run nba_bootstrap   -t <target>

# 9. Deploy again — now app-writes + endpoint exist, so the app is created.
databricks bundle deploy   -t <target>

# 10. Re-run bootstrap: the app now exists, so its service principal is
#     auto-resolved and granted Lakebase access.
databricks bundle run nba_bootstrap   -t <target>

# 11. Deploy the app's source code and start it
databricks bundle run nba_console     -t <target>

# 12. CDF is already enabled — nba_bootstrap (steps 8/10) turns on Lakebase CDF
#     for the app-writes branch automatically via the Lakebase CDF API. No manual
#     step. (If that API is unavailable in your workspace, the bootstrap output
#     prints the one-time manual UI click-path as a fallback.)

# 13. Verify the publish loop: edit an action in the app (Manage Actions), then
databricks bundle run nba_reconcile   -t <target>   # CDF -> UC -> production
```

> **Prefer one command?** [`scripts/setup.sh <target>`](scripts/README.md) runs
> steps 5–11 for you (CDF auto-enabled and verified); then
> `scripts/demo.sh <target>` runs step 13 as a narrated demo.

The app auto-resolves its own service principal for Lakebase grants — no client
id to copy around. Override any variable inline, e.g.
`databricks bundle deploy -t <target> --var="uc_catalog=my_catalog"`.

### Verified end-to-end

This exact sequence has been run end-to-end against a live Azure Databricks
workspace from an empty catalog: all jobs succeeded, the serving endpoint
reached `READY`, the app reached `RUNNING`, and the full app-edit → Lakebase CDF
→ `nba_reconcile` → UC → production loop was verified (inserts, updates, and
deletes), including via `scripts/setup.sh` + `scripts/demo.sh`.

## Configuration reference

Every value below is a bundle variable (`databricks.yml`), passed to notebooks
as widgets and to the app as env vars.

| Variable | Default | Description |
| --- | --- | --- |
| `uc_catalog` | `nba_demo` | Unity Catalog catalog |
| `uc_schema` | `nba_new` | UC schema with source tables |
| `lakebase_project` | `nba-lakebase` | Lakebase project id |
| `lakebase_database` | `databricks_postgres` | Postgres database name (SQL dbname) |
| `lakebase_database_id` | `databricks-postgres` | DB **resource id** for app paths (often hyphenated) |
| `lakebase_schema` | `nba_new_lbase` | Postgres schema for NBA tables |
| `lakebase_branch` | `production` | Read-path branch |
| `app_writes_branch` | `app-writes` | CRUD-path branch |
| `model_name` | `nba_scoring_model` | Registered model (within catalog.schema) |
| `model_endpoint_name` | `nba-scoring-endpoint` | Serving endpoint name |
| `mlflow_experiment_path` | *(blank)* | Training experiment path; blank → running user's home |
| `app_name` | `nba-console` | Databricks App name (+ SP auto-resolve) |
| `nba_console_sp` | *(blank)* | App SP client id; blank → auto-resolve |

## Operational runbook (day-2)

Two **independent** cadences run the system after install. They never conflict
because each owns a different table (see [SETUP.md](SETUP.md#day-2-operations--the-ongoing-lifecycle)
for the full lifecycle + parameter matrix).

| Task | Command | Frequency | Touches |
| --- | --- | --- | --- |
| Refresh member features | `databricks bundle run nba_daily_sync -t <t>` | Daily (job scheduled, paused by default) | `member_features` only |
| Publish app edits to production | `databricks bundle run nba_reconcile -t <t>` | After a business user adds/edits an action | `action_catalog` only |
| Retrain + redeploy model | `databricks bundle run nba_train_model -t <t>` | When feature *definitions* change | model + endpoint |
| Full environment reset (destructive) | `databricks bundle run nba_bootstrap -t <t>` | Ad-hoc / demo reset | everything |

**The edit→publish loop (CDF):** a business user adds an action in the app → it
stages on the **permanent** `app-writes` branch (scoring unaffected) →
**Lakebase Change Data Feed** captures the change into a UC history table → run
`nba_reconcile` → it reads the new CDF rows since the last LSN watermark, MERGEs
them into UC, **and re-syncs the production branch in the same run**, so the edit
goes live for scoring. You do **not** run a separate catalog refresh after
reconcile, and there is **no branch re-fork** — the app-writes branch is
permanent and the LSN watermark tracks progress. `nba_daily_sync` never touches
`action_catalog`, so the two jobs are safe to run in any order.

> **The regular loop is exactly two commands** — `nba_daily_sync` (member
> features) and `nba_reconcile` (action edits). Neither is destructive; neither
> requires a manual step. Setup and reset are automated too — see the
> [reset flow](#reset-flow-rare) below.

### Reset flow (rare)

A reset is the **only** operation that recreates the app-writes branch, and CDF
does not survive a re-fork — so CDF must be re-enabled after. `nba_bootstrap`
(`reset_environment=true`) rebuilds everything and **re-enables Lakebase CDF
automatically via the Lakebase CDF API**, then verifies the feed is streaming —
no manual step. (If the CDF API is unavailable in your workspace, the bootstrap
output prints the one-time manual UI click-path — Compute → Lakebase → your
project → branch `app-writes` → **Lakebase CDF** tab → add
`<lakebase_schema>.action_catalog` → destination `<cdf_catalog>.<cdf_schema>`
→ Start.) The same automation runs on the first-ever bootstrap when the branch is
created. Until CDF is enabled, `nba_reconcile` correctly reports "no changes".

### How reconcile applies only the correct records

The CDF history table (`<cdf_catalog>.<cdf_schema>.lb_action_catalog_history`) is
**append-only** — it keeps *every* insert/update/delete ever captured. `nba_reconcile`
still applies exactly the right rows through three mechanisms working together
(see `design/CDF_reconciliation_design.md` §6–§7 for the full detail):

1. **LSN watermark — process only what's new.** A tiny UC table
   (`action_catalog_watermark`) stores the highest `_pg_lsn` already processed.
   `_pg_lsn` is Postgres's Log Sequence Number: strictly monotonic and
   ever-increasing. Each run reads only `WHERE _pg_lsn > last_lsn`, so all older
   history is ignored — the run sees just the slice of changes since last time.

2. **Collapse to newest-per-key — apply only the final state.** Within that new
   slice, one `action_id` may have several events (insert, then two edits). A
   window function keeps only the latest per key:
   `ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY _pg_lsn DESC, _sort_by DESC) = 1`
   (`_sort_by` breaks ties within the same LSN). So multiple edits collapse to the
   one end state — never replayed one-by-one.

3. **Change-type resolution — inserts/updates/deletes land correctly.**
   `update_preimage` rows (the *old* values CDF emits before an update) are
   dropped — they never define final state. The winning row's `_pg_change_type`
   drives a single `MERGE`: `delete` → DELETE the target row; otherwise UPSERT.

**Idempotent + restart-safe.** The watermark advances **only at the very end**,
after the MERGE into UC *and* the production sync both succeed. If the job crashes
mid-run the watermark is unchanged, so the next run reprocesses the same slice
harmlessly (MERGE to end-state is idempotent). Re-running with nothing new exits
`NO_CHANGES`. A reset guard also auto-resets the watermark to `-1` if it is ever
higher than anything in a freshly re-created history table, so a rebuilt feed is
fully reprocessed instead of silently skipped.

> **Net-state, not event replay.** This reconciles *current state* (what
> production scoring needs), so if an action is created **and** deleted entirely
> between two runs, the delete wins and it correctly never lands in UC — the
> intermediate insert is not applied to UC. The full blow-by-blow history remains
> in the CDF table if you ever need to audit it.

## Key design decisions

* **Fully configurable** — one bundle, many environments; change variables, not code.
* **Branch isolation** — business users edit `action_catalog` on `app-writes`
  without affecting production scoring.
* **Copy-on-write** — branches share storage; forking is instant and free.
* **Change capture via Lakebase CDF** — a managed Change Data Feed persists every
  INSERT/UPDATE/DELETE on `action_catalog` to a UC history table; `nba_reconcile`
  consumes it incrementally by LSN watermark. No trigger, no change-log table, no
  branch re-fork. CDF is enabled programmatically via the Lakebase CDF API during
  bootstrap (with a manual-UI fallback), so the whole install is hands-off.
* **Dynamic host resolution** — notebooks and app resolve Lakebase hosts via the
  SDK, so they self-heal across host changes with no redeploy.
* **SP auto-resolution** — the sync/reconcile notebooks discover the app's
  service principal from the deployed app, so grants need no manual client id.
* **Daily member refresh mirrors production** — the daily job refreshes only
  `member_features`, matching a real batch feature pipeline that recomputes
  member signals nightly; actions change only on deliberate publish.
* **Action-agnostic model + scale-to-zero serving** — `nba_train_model`
  registers the LightGBM model to UC and deploys `nba-scoring-endpoint`
  (CPU Small, scale-to-zero). Because the model scores any member × action pair
  from features, newly published actions are scored immediately with **no
  retraining**. Scale-to-zero means the endpoint costs nothing when idle and
  cold-starts on first request (the app has wake-up retry logic).

## License

[MIT](LICENSE)
