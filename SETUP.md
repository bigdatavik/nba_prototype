# Deployment Guide

Deploy the NBA Prototype to any Databricks workspace using the Databricks Asset
Bundle. All environment-specific values are **bundle variables** — you never
edit notebook or app code to change workspace, catalog, Lakebase project, or
endpoint names.

## TL;DR — the short version

```bash
# 1. Log in
databricks auth login --host https://<your-workspace> --profile <profile>
export DATABRICKS_CONFIG_PROFILE=<profile>

# 2. Configure the bundle (the bundle creates the Lakebase project itself)
cp databricks.yml.template databricks.yml     # edit host + variables

# 3. Install everything (deploy, seed, train, bootstrap, app, CDF) — one command
./scripts/setup.sh <target>

# 4. Try the loop
./scripts/demo.sh <target>
```

Everything below explains each step in detail. **If `setup.sh` worked, you can
stop after step 3** — the manual steps are what the script automates, kept here
for understanding, debugging, and CI.

## Prerequisites

- Databricks CLI `v0.287.0+` (`databricks -v`) — needed for the `postgres_*`
  bundle resources that create the Lakebase project (v0.239.0 is the minimum for
  Apps in bundles).
- A workspace with Unity Catalog, Databricks Apps, and Lakebase enabled
- Authenticated CLI: `databricks auth login --host https://<workspace-host>`

The Lakebase project is **created by the bundle** — no manual project step.

> No Claude/AI tooling is needed to install or run this project. `CLAUDE.md` is
> optional context for Claude Code users and can be ignored.

> **Shortcut:** [`scripts/setup.sh <target>`](scripts/README.md) automates
> Steps 4–12 below (deploy → seed → train → bootstrap → app → auto-enable CDF).
> `scripts/demo.sh` and `scripts/reset.sh` cover the demo + clean-slate flows.
> The manual steps below are what those scripts run.

## Step 0 — Clone

```bash
git clone <repo-url>
cd nba_prototype
```

## Step 1 — Lakebase project (created by the bundle)

**Nothing to do by hand.** The bundle defines the Lakebase project as a
`postgres_projects` resource (`resources/nba_lakebase.yml`), so `databricks
bundle deploy` (Step 4) creates the project and its `production` branch +
endpoint automatically. Just set the `lakebase_project` variable to the id you
want (Step 3). Requires CLI v0.287.0+.

> The project has `prevent_destroy: true` so a stray `bundle destroy` can't
> delete it. To tear it down deliberately, remove that guard, or run
> `databricks postgres delete-project projects/<id> --purge`.

## Step 2 — Unity Catalog source tables

The bundle **creates these for you** with synthetic data in Step 6
(`nba_seed_data`), so you don't need to build them by hand:

| Table | Description |
| --- | --- |
| `serving_member_features` | 50 members × 30 numeric features (model input) |
| `action_catalog` | 16 NBA actions with metadata (governed source of truth) |

To use **your own** data instead, create these tables yourself and skip
`nba_seed_data`. No special table properties are required on the UC tables —
change capture is handled by **Lakebase CDF on the app-writes branch** (a
one-time UI step in Step 11), not by Delta CDF on the UC table.

## Step 3 — Configure the bundle

Create your bundle config from the committed template (the real `databricks.yml`
is gitignored so environment-specific names never get pushed):

```bash
cp databricks.yml.template databricks.yml
```

Then edit `databricks.yml` (or override on the CLI). Set the workspace host per
target and adjust variables:

```yaml
targets:
  dev:
    workspace:
      host: https://<your-dev-workspace>.cloud.databricks.com
    variables:
      uc_catalog: <catalog>
      uc_schema: <schema>
      lakebase_project: <your-lakebase-project-id>
      lakebase_schema: nba_new_lbase
      app_name: nba-console-dev
```

Validate:

```bash
databricks bundle validate -t dev
```

## Step 4 — Deploy (first pass: jobs)

```bash
databricks bundle deploy -t dev
```

This uploads the notebooks and creates the five jobs. **The `nba-console` app
step will error on this first deploy** with something like
`Failed to get postgres branch ...` — this is expected. The app binds to the
Lakebase `app-writes` branch and the model endpoint, which don't exist yet. We
create them in Steps 5–7, then deploy again in Step 8 to create the app.

> **Note on the app's Lakebase binding.** In the DABs schema the app uses a
> `postgres:` resource whose `branch` and `database` are **full resource paths**
> (`projects/<proj>/branches/<branch>` and
> `.../databases/<database_id>`). The database *resource id* is often the
> hyphenated form of the dbname (e.g. `databricks-postgres` for the
> `databricks_postgres` database) — set it via the `lakebase_database_id`
> variable. Find it with:
> `databricks postgres list-databases projects/<proj>/branches/production`.

## Step 5 — Seed the UC source tables

```bash
databricks bundle run nba_seed_data -t dev
```

Runs `seed_uc_source_tables`, which creates the catalog/schema (if needed) and
writes synthetic `serving_member_features` (50 members) and `action_catalog`
(16 actions). Skip this if you brought your own source tables. (Change capture
for reconciliation is Lakebase CDF on the app-writes branch — Step 11 — not a
property of the UC table.)

## Step 6 — Train and deploy the model

```bash
databricks bundle run nba_train_model -t dev
```

Trains a LightGBM ranker on `serving_member_features` × `action_catalog`,
registers it to `<catalog>.<schema>.<model_name>`, and deploys the
`<model_endpoint_name>` serving endpoint (CPU Small, scale-to-zero). The MLflow
experiment defaults to the running user's home folder.

## Step 7 — Bootstrap Lakebase (creates the app-writes branch)

```bash
databricks bundle run nba_bootstrap -t dev
```

This runs `sync_nba_to_lakebase` with `reset_environment=true` and
`refresh_action_catalog=true`, which:

1. Creates the Postgres schema
2. Syncs `member_features` and `action_catalog`
3. Creates point-lookup indexes and sets `REPLICA IDENTITY FULL` on
   `action_catalog` automatically (so CDF carries full row images — never a
   manual SQL step)
4. Forks the **permanent** `app-writes` branch from production
5. Creates the CDF destination schema + watermark table in Unity Catalog
6. Grants the app's service principal read/write access — **skipped on this
   pass** because the app doesn't exist yet (you'll see a "No app SP configured"
   message). That's expected; Step 9 does the grant.

At the end it **enables Lakebase CDF automatically** on the new `app-writes`
branch (via the Lakebase CDF API) and verifies the feed is streaming — no manual
step. If that API is unavailable in your workspace, the bootstrap output prints
the one-time manual UI click-path as a fallback (see Step 11).

> A legacy audit trigger + `_action_catalog_changes` table are also created for
> parallel-run validation against the old design; they are not used by the CDF
> reconcile and will be removed after cutover.

## Step 8 — Deploy again (second pass: the app is created)

Now that the `app-writes` branch and the serving endpoint exist, the app's
resource bindings resolve:

```bash
databricks bundle deploy -t dev
```

`Deployment complete!` — the `nba-console` app now exists (compute STOPPED).

## Step 9 — Bootstrap once more (grant the app SP)

```bash
databricks bundle run nba_bootstrap -t dev
```

This time the notebook **auto-resolves the app's service principal** from the
deployed app (matched by `app_name`) and grants it Lakebase access on both
branches. No client id to copy. To pin one explicitly, set the `nba_console_sp`
variable.

## Step 10 — Deploy the app code and start it

```bash
databricks bundle run nba_console -t dev
```

This syncs the app source, installs requirements, and starts Streamlit. When it
prints `App started successfully` with a URL, open it (Databricks OAuth login
required) and verify:

1. **Member Lookup** → select a member → features + scored actions appear
2. **Manage Actions** → add/edit/delete → the edit stages on the app-writes branch

## Step 11 — CDF (enabled automatically; manual fallback)

**In most workspaces there is nothing to do here** — `nba_bootstrap` (Steps 7/9)
already enabled Lakebase CDF for the `app-writes` branch via the Lakebase CDF API
and verified it is streaming. You can confirm with:

```sql
SELECT _pg_change_type, count(*)
FROM <cdf_catalog>.<cdf_schema>.lb_action_catalog_history GROUP BY 1;
```

**Fallback (only if the CDF API is unavailable in your workspace):** the bootstrap
output will say so and print the manual UI click-path. Enable it once per
app-writes fork (CDF does not survive a re-fork):

1. **Compute → Lakebase →** your project → branch **`app-writes`** → **Lakebase CDF** tab
2. Click **Start** (if disabled)
3. Under **Tables**, add:
   - source: `<lakebase_schema>.action_catalog`
   - destination: catalog `<cdf_catalog>`, schema `<cdf_schema>` (table auto-named `lb_action_catalog_history`)
4. Confirm status shows **Enabled** with a **Committed LSN**

## Step 12 — Run the reconcile loop

```bash
databricks bundle run nba_reconcile -t dev
```

The edit made in Step 10 flows CDF → UC → production and goes live for scoring.
Re-open **Member Lookup**: the new/edited action now participates in scoring (no
model retrain — the model is action-agnostic). From here, steady state is just the
two-command loop: `nba_daily_sync` (features) + `nba_reconcile` (action edits).

## Day-2 operations — the ongoing lifecycle

After the one-time install, the system runs on **two independent cadences**.
Understanding which job owns which table is the key to not clobbering data.

### Who owns what

| Table | Owned / updated by | Never touched by |
| --- | --- | --- |
| `member_features` | `nba_daily_sync` (from the UC feature pipeline) | `nba_reconcile` |
| `action_catalog` | `nba_reconcile` (publishes app edits UC → production) | `nba_daily_sync` |

This separation is intentional: `nba_daily_sync` refreshes **only**
`member_features` and deliberately leaves `action_catalog` alone, so a daily
data refresh can never overwrite business edits that were published via
reconcile. Conversely, `nba_reconcile` re-syncs **only** `action_catalog`.

### The `sync_nba_to_lakebase` parameter matrix

Both day-2 sync flavors are the same notebook with different parameters:

| `reset_environment` | `refresh_action_catalog` | Effect | Job |
| :---: | :---: | --- | --- |
| `false` | `false` | Sync **member_features only**. `action_catalog` untouched. | `nba_daily_sync` |
| `false` | `true` | Sync member_features **+** reset `action_catalog` to the 16 baseline rows (app-added rows dropped). No branch changes. | `nba_reset_action_catalog` |
| `true` | *(ignored)* | **Destructive & rare**: delete app-writes branch, drop all prod tables, rebuild everything, re-fork a fresh app-writes branch, re-grant, drop the CDF history table + reset the watermark, then **re-enable CDF automatically via the API** (manual-UI fallback if unavailable). | `nba_bootstrap` |

> ⚠️ Only `nba_bootstrap` (`reset_environment=true`) is destructive. The daily
> job is always `false/false`.

### Flow A — Daily member-features refresh (scheduled)

Run every morning to pull the latest feature values from the UC feature
pipeline into the production branch:

```bash
databricks bundle run nba_daily_sync -t <target>   # reset=false, refresh_catalog=false
```

**Why only member_features?** This mirrors the real world. In production, a
batch feature pipeline recomputes member features **every day** — new claims,
interactions, satisfaction scores, and risk propensities land overnight — so the
serving layer must refresh member data daily to score on current signals. The
`action_catalog`, by contrast, changes only when the business *deliberately*
edits and publishes it (Flow B). Keeping the daily job to `member_features`
only makes the demo faithful to that operational cadence — and guarantees a
daily data refresh can never overwrite a published action edit.

The `nba_daily_sync` job ships with a 6am schedule, **paused** by default.
Unpause it in the Jobs UI, or set `pause_status: UNPAUSED` in
`resources/nba_jobs.yml` and redeploy.

### Flow B — Business user edits an action, then you publish it

This is the CRUD → governance loop. Example: a clinical-ops user opens the app,
goes to **Manage Actions**, and adds a new action (or edits/deletes one).

1. **The edit lands on the `app-writes` branch only.** The app writes to the
   **permanent** `app-writes` branch; **Lakebase Change Data Feed (CDF)** captures
   the INSERT/UPDATE/DELETE into a UC history table
   (`<cdf_catalog>.<cdf_schema>.lb_action_catalog_history`). **Production scoring is
   unaffected** — the new action does *not* appear in Member Lookup yet. This is
   the staging isolation.

2. **Publish the edit — run reconcile:**
   ```bash
   databricks bundle run nba_reconcile -t <target>
   ```
   `reconcile_action_catalog` does all of this in one run:
   - reads the CDF history table for rows newer than the last **LSN watermark**
   - collapses to the newest change per `action_id` (net insert/update/delete)
   - **MERGE**s the net changes into the governed UC Delta table
     (`<catalog>.<schema>.action_catalog`)
   - **re-syncs the production branch** `action_catalog` from UC — *this is the
     step that makes the new action live for scoring*
   - **advances the LSN watermark** so the next run is incremental and idempotent

   There is **no branch re-fork** — the app-writes branch is permanent and CDF
   keeps flowing.

3. **You do NOT need a catalog refresh afterward.** Reconcile already re-synced
   production's `action_catalog` (bullet 3 above). Running `nba_daily_sync`
   after reconcile is safe because it never touches `action_catalog`.

> **Ordering rule of thumb:** `nba_reconcile` is self-contained for actions.
> `nba_daily_sync` is self-contained for member features. Run them independently
> whenever each is needed; they don't conflict and have no required order.

### Putting it together — a typical demo day

```bash
# Morning: refresh member data (or let the 6am schedule do it)
databricks bundle run nba_daily_sync -t <target>

# A business user adds/edits an action in the app (Manage Actions page).
# It's staged on app-writes; Member Lookup scoring is NOT yet affected.

# When the edit is approved, publish it:
databricks bundle run nba_reconcile -t <target>
# → new/edited action is now MERGEd into UC and live on the production branch.
#   Re-open Member Lookup: the action now participates in scoring (no model
#   retrain needed — the model is action-agnostic).
```

### When to use the destructive reset

Only to rebuild a broken/dirty environment or reset a demo to a clean baseline.
This is the **one** flow that recreates the app-writes branch, so it is also the
**one** flow with a manual follow-up step:

```bash
databricks bundle run nba_bootstrap -t <target>    # reset_environment=true
```

It re-forks the `app-writes` branch and **re-enables CDF automatically via the
Lakebase CDF API**, then verifies streaming — no manual step. (CDF does not
survive a re-fork, which is why the reset re-enables it.) The watermark is reset
to `-1`, so the first reconcile after the reset picks up the whole fresh feed.

If the CDF API is unavailable in your workspace, the notebook output says so and
prints the one-time manual UI click-path (Compute → Lakebase → project → branch
`app-writes` → **Lakebase CDF** tab → source `<lakebase_schema>.action_catalog` →
destination catalog `<cdf_catalog>`, schema `<cdf_schema>` → Start).

To reset just the action catalog to its 16 baseline rows **without** touching
branches (drops app-added rows, keeps the app-writes branch and CDF running):

```bash
databricks bundle run nba_reset_action_catalog -t <target>   # reset=false, refresh_catalog=true
```

## Tearing down the whole environment

To remove everything and start clean:

```bash
./scripts/destroy.sh <target>      # confirms, then wipes; --yes to skip the prompt
./scripts/setup.sh   <target>      # reinstall from scratch
```

`destroy.sh` cleans the workspace in three steps: removes the **app + all jobs**
(`bundle destroy`), **purges the Lakebase project** (hard delete via
`DELETE /api/2.0/postgres/projects/<id>?purge=true`, so the name is reusable
immediately), and **drops the Unity Catalog data** (by default the two project
schemas `<uc_catalog>.<uc_schema>` and `<cdf_catalog>.<cdf_schema>`). Gotchas it
handles for you:

- The project resource carries `prevent_destroy: true` (guards against an
  accidental `bundle destroy`). The script temporarily lifts it, then restores
  the file unchanged.
- Plain `bundle destroy` / `databricks postgres delete-project` only **soft-delete**
  a project (7-day retention, and you can't reuse the name during that window).
  The `?purge=true` REST call hard-deletes immediately.
- UC cleanup runs on a SQL warehouse the script auto-finds.

Flags:

| Flag | Effect |
| --- | --- |
| *(none)* | drop the two project schemas |
| `--drop-catalog` | drop the entire `<uc_catalog>` |
| `--keep-catalog` | leave UC data untouched |
| `--keep-project` | leave the Lakebase project in place |
| `--yes` | skip the confirmation prompt |

> **Timing:** the teardown is ~1–2 min. Only the **reinstall** (`setup.sh`) takes
> ~15 min, almost all of it model training.

## Promoting to production

```bash
databricks bundle deploy -t prod
```

The `prod` target runs in `mode: production`. Set its `workspace.host`,
`root_path`, and optionally `run_as.service_principal_name` in `databricks.yml`.

## Troubleshooting

| Issue | Fix |
| --- | --- |
| `is not a notebook` on deploy | Ensure notebook files start with `# Databricks notebook source` |
| `failed to create app` / `Failed to get postgres branch` on first deploy | Expected — app-writes branch/endpoint don't exist yet. Run Steps 5–7, then deploy again (Step 8). |
| `Field 'name' expects 'projects/.../branches/...'` | The app `postgres.branch`/`database` must be full resource paths. Fixed in `resources/nba_app.app.yml`; set `lakebase_database_id` (often the hyphenated dbname). |
| `zero-length delimited identifier` on GRANT | The app SP was blank (app not deployed). Deploy the app, then re-run `nba_bootstrap`. |
| App can't read production tables | Re-run `nba_bootstrap` after the app exists (SP grants are idempotent) |
| App-writes branch missing | Run `nba_bootstrap` (creates it) |
| SP grants skipped | Deploy the app first, then re-run `nba_bootstrap`, or set the `nba_console_sp` variable |
| SDK `AttributeError` on `w.postgres` | Notebooks `%pip install "databricks-sdk>=0.118.0"` automatically |
| prod validate: `must set workspace.root_path` | Already set in `databricks.yml`; adjust owner |
| Browser: **`ERR_NAME_NOT_RESOLVED` / "site can't be reached"** for the app URL, even though the app shows **Running** in the Apps UI | Stale DNS **negative cache**. The `databricksapps.com` zone has a 24h negative-cache TTL, so if you **destroyed then recreated** the app, your machine/browser may still be serving the old "no such host". The app is fine — clear the cache: **Chrome** `chrome://net-internals/#dns` → *Clear host cache* (also `#sockets` → *Flush socket pools*); **macOS** `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`; or set your Wi-Fi DNS to `8.8.8.8`. A first-time deploy (never destroyed) does not hit this. |
