# CLAUDE.md — NBA Prototype

Guidance for Claude Code (and humans) working in this repository.

## What this project is

A real-time **Next Best Action (NBA)** engine for a healthcare payer, built on
Databricks. It is packaged as a **Databricks Asset Bundle (DABs)** so the same
code deploys to any workspace by changing configuration only.

Components:
- **Notebooks** (`src/notebooks/`) — UC → Lakebase sync, reconciliation, and
  model training/deployment.
- **Streamlit app** (`src/app/`) — the `nba-console`. Pages: **Member Lookup**
  (score + rank, plus a per-member **🧠 Assist**: *Why this action* reason codes /
  score-vs-threshold / trajectory, LLM **Draft outreach**, **What-if** re-score,
  and **Approve & act** — a human approves/edits a recommendation, committed to
  the app-writes Postgres branch), **✅ Decisions** (Approve & Act audit + outcome
  capture), **Manage Actions**, **Change Log**, and **💬 Ask NBA** (Genie
  Conversation API over UC for population analytics).
- **Bundle** (`databricks.yml`, `resources/`) — jobs + app, parameterized by
  variables per target (`dev`, `prod`). The real `databricks.yml` is **gitignored**;
  only `databricks.yml.template` is committed. Copy it (`cp databricks.yml.template
  databricks.yml`) and fill in host/variables. Never commit real workspace hosts,
  catalog names, or customer identifiers — keep them in the local `databricks.yml`.

Data flow: Unity Catalog (source of truth) → Lakebase `production` branch (read
path) and `app-writes` branch (CRUD path) → app scores via a Model Serving
endpoint. See `README.md` for the architecture diagram.

## Golden rule: nothing is hardcoded

Every workspace-, catalog-, Lakebase-, endpoint-, or SP-specific value is a
**bundle variable** in `databricks.yml`, passed to:
- notebooks as **widgets** (`dbutils.widgets.get(...)`), wired via
  `resources/nba_jobs.yml` `base_parameters`
- the app as **env vars** (`os.getenv(...)`), wired via `resources/nba_app.app.yml`
  `config.env` (which overrides the defaults in `src/app/app.yaml`)

When adding a config knob: add it to `databricks.yml` `variables:` → reference
it in `resources/*.yml` → read it from a widget or env var. **Do not** put a
workspace host, catalog name, Lakebase project id, endpoint host, or service
principal id directly in `src/`.

## Conventions

- **Notebooks are Databricks source files.** Each starts with
  `# Databricks notebook source`, cells split by `# COMMAND ----------`, markdown
  as `# MAGIC %md`. DABs rejects `.py` files that aren't valid notebooks.
- **Lakebase hosts are resolved at runtime** via the SDK
  (`w.postgres.get_endpoint`), never hardcoded — this survives branch re-forks.
- **The app service principal is auto-resolved** from the deployed app by name
  (the `app_name` variable); `nba_console_sp` can pin it explicitly.
- The `docs/` directory (planning/article notebooks) is **gitignored** on purpose.

## Common commands

```bash
# Validate (do this before every deploy)
databricks bundle validate -t dev
databricks bundle validate -t prod

# Deploy jobs + app
databricks bundle deploy -t dev

# Run resources
databricks bundle run nba_train_model -t dev   # train + deploy endpoint
databricks bundle run nba_bootstrap   -t dev   # reset + seed Lakebase
databricks bundle run nba_reconcile   -t dev   # publish app edits UC → production (day-2)
databricks bundle run nba_daily_sync  -t dev   # refresh member_features only (day-2)
databricks bundle run nba_reset_action_catalog -t dev  # actions → 16 baseline rows (non-destructive)
databricks bundle run nba_seed_decisions -t dev  # generate dummy Approve & Act decisions (demo volume)
databricks bundle run nba_reconcile_decisions -t dev  # publish decisions Lakebase CDF → governed UC (day-2)
databricks bundle run nba_console     -t dev   # start the app

# App logs
databricks apps logs <app-name>
```

## Voice commands — say it in plain English, I run it

**Instruction to the assistant:** when the user says one of the phrases below (or
anything close to it), do this: (1) print one line — `Running: <the command>`,
(2) run that exact command with the Bash tool on the `azure` target, (3) report
the result plainly. Don't ask for confirmation for the **Day-to-day** commands;
just announce and run. For the **Initial setup or reset** commands (they rebuild
the environment), confirm first before running.

Set `export DATABRICKS_CONFIG_PROFILE=<your-cli-profile>` for raw CLI verification;
`databricks bundle …` commands infer the host from `databricks.yml`. Swap the
target name for another target if the user names one.

### Day-to-day (announce and run immediately)

| If the user says… | Print `Running:` and run |
| --- | --- |
| "reset the demo" / "clean slate" / "start fresh" | `./scripts/reset.sh azure` |
| "run the demo" / "show the reconcile demo" | `./scripts/demo.sh azure` |
| "run the reconcile" / "publish my edit" / "I added an action" | `databricks bundle run nba_reconcile -t azure` |
| "run the member refresh" / "daily sync" / "refresh features" | `databricks bundle run nba_daily_sync -t azure` |
| "reset the actions" / "actions back to baseline" | `databricks bundle run nba_reset_action_catalog -t azure` |
| "clean up the demo rows" | `./scripts/demo.sh azure --cleanup && databricks bundle run nba_reconcile -t azure` |
| "start the app" / "restart the app" | `databricks bundle run nba_console -t azure` |
| "deploy" / "push my code changes" | `databricks bundle deploy -t azure` |
| "check CDF" / "is CDF on" | `databricks api get /api/2.0/postgres/projects/<proj>/branches/<app_writes>/databases/<db_id>/cdf-configs/<lakebase_schema>/cdf-statuses` |

### Initial setup or reset (confirm first — these rebuild the environment)

| If the user says… | Print `Running:` and run |
| --- | --- |
| "full install" / "install everything" | `./scripts/setup.sh azure` |
| "full rebuild" / "nuke and rebuild" | `./scripts/reset.sh azure --full` |
| "tear down everything" / "destroy it all" / "delete the environment" | `./scripts/destroy.sh azure` |

> These scripts are config-driven (`scripts/README.md`) and work in any workspace.
> `reset.sh` (day-to-day) is non-destructive (actions → baseline, keeps branch + CDF).
> `setup.sh` / `reset.sh --full` recreate the branch and auto-re-enable CDF via API.
> `destroy.sh` cleans the workspace: removes app + jobs, purges the Lakebase
> project (hard delete, name reusable now), and drops the UC schemas (default) or
> the whole catalog (`--drop-catalog`). Flags: `--keep-catalog`, `--keep-project`,
> `--yes`. Teardown ~1-2 min; reinstall (`setup.sh`) ~15 min (mostly model training).
> Reinstall with `setup.sh`.

**Manual install sequence** (what `setup.sh` automates): `deploy` → `nba_seed_data`
→ `nba_train_model` → `nba_bootstrap` (also auto-enables CDF via the Lakebase CDF
API) → `deploy` (app) → `nba_bootstrap` (grant SP) → `nba_console` → `nba_reconcile`.
No manual step. (If the CDF API is unavailable in a workspace, bootstrap prints
the one-time manual UI click-path as a fallback.)

**The demo story to tell:** business user edits an action in the app (stages on
`app-writes`, scoring unaffected) → Lakebase CDF captures it to UC (~<30s) →
`nba_reconcile` MERGEs to UC + re-syncs production → action is live for scoring
(no model retrain — the model is action-agnostic).

**Demo timing tip:** wait ~30s after saving in the app before `nba_reconcile`, or
it may exit `NO_CHANGES` (CDF hasn't captured yet — just wait and re-run).

## Local checks before a PR

```bash
databricks bundle validate -t dev
databricks bundle validate -t prod
python -m py_compile src/app/app.py
```

## Gotchas

- **Production mode** requires `workspace.root_path` (already set in
  `databricks.yml`); adjust the owner for your prod principal.
- **App resource schema** in DABs uses `postgres:` (not `lakebase:`) and
  `serving_endpoint` needs `name` + `permission` — differs from the runtime
  `app.yaml` schema.
- **The Lakebase project is now bundle-created** (`resources/nba_lakebase.yml`,
  `postgres_projects`, CLI >= 0.287.0) with `prevent_destroy: true` — no manual
  project step. The UC catalog/schema + synthetic source tables are created by
  the `nba_seed_data` job. The `app-writes` branch is still created by
  `nba_bootstrap` (it owns the CDF + REPLICA IDENTITY logic and re-forks on
  reset), so the bundle owns only the project + auto `production` branch. See `SETUP.md`.
- **Tearing down the project / purge:** `bundle destroy` only **soft-deletes** a
  project (7-day retention; blocks reusing the name). Our CLI (v0.298.0) has **no
  `--purge` flag** on `postgres delete-project` — to hard-delete immediately (free
  the name now) use the REST API:
  `databricks api delete "/api/2.0/postgres/projects/<id>?purge=true"`.
  `scripts/destroy.sh` does the full safe teardown (temporarily lifts the
  prevent_destroy guard → `bundle destroy` app+jobs → purges the project via REST).
- **Fresh-install order**: deploy → `nba_seed_data` → `nba_train_model` →
  `nba_bootstrap` → `nba_console`.
- **Day-2 lifecycle** (two independent cadences, no required order):
  `nba_daily_sync` owns `member_features` (never touches actions);
  `nba_reconcile` owns `action_catalog` (reads the **Lakebase CDF** history table
  by LSN watermark → MERGE net changes to UC → re-sync production; **no re-fork**,
  the app-writes branch is permanent). After a business user edits an action in
  the app, run `nba_reconcile` — it re-syncs production itself, so no separate
  catalog refresh is needed. `reset_environment=true` (`nba_bootstrap`) is the
  only destructive job. **`nba_reconcile_decisions`** owns `nba_decisions` (the
  app's **Approve & Act** decisions): reads the Lakebase CDF
  `lb_nba_decisions_history` by LSN watermark → MERGEs net-state into the governed
  UC `nba_decisions` (the *learning half* of the closed loop). **No production
  re-sync** — decisions are operational app state, written to app-writes and read
  back there. `nba_seed_decisions` generates dummy decisions for demo volume.
- **Decisions table (`nba_decisions`)** lives on the **app-writes** Postgres
  branch (writable; never the read-only synced tables). Bootstrap pre-creates it
  with **`REPLICA IDENTITY FULL`** (the requirement for Lakebase CDF to capture a
  table, same as `action_catalog`) and grants the app SP **`CREATE ON SCHEMA`** so
  it can own its operational tables. The schema-level CDF config then replicates
  it to UC as `<cdf_catalog>.<cdf_schema>.lb_nba_decisions_history`.
- **Genie / "Ask NBA"**: the Genie **space** is provisioned by a *separate* project
  (`~/nba_payer_genie_tutorial`, its own bundle) over UC; the app just references
  it via the `genie_space_id` variable → `GENIE_SPACE_ID` env var. `setup.sh`
  grants the app SP everything Genie needs (space CAN_RUN, warehouse CAN_USE, UC
  SELECT). The Assist **Draft outreach** uses a Foundation Model chat endpoint via
  the `llm_endpoint_name` variable → `LLM_ENDPOINT_NAME` (blank disables drafting).
- **Databricks SDK** must be `>= 0.118.0` for `w.postgres.*`; notebooks `%pip`
  install it and restart Python automatically.
- **App URL `ERR_NAME_NOT_RESOLVED` after a destroy+recreate** is NOT an app
  failure — it's stale DNS **negative cache** (the databricksapps zone has a 24h
  negative TTL). If the Apps UI shows Running but the browser can't reach it,
  the app is healthy (verify: `curl --resolve <host>:443:<workspace-ip>` → 200).
  Fix client-side: Chrome `chrome://net-internals/#dns` Clear host cache, macOS
  `dscacheutil -flushcache; killall -HUP mDNSResponder`, or DNS 8.8.8.8.
  `setup.sh` now detects this and prints the guidance; a first-time deploy
  (never destroyed) doesn't hit it.

