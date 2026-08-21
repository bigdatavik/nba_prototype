# Contributing

Thanks for your interest in improving the NBA Prototype.

## Ground rules

- **Keep it configurable.** No workspace hosts, catalog names, Lakebase project
  ids, endpoint hosts, or service principal ids hardcoded in `src/`. Add a
  bundle variable in `databricks.yml`, wire it through `resources/*.yml`, and
  read it from a notebook widget or app env var.
- **Notebooks are Databricks source files.** Each must start with
  `# Databricks notebook source`, use `# COMMAND ----------` cell separators, and
  `# MAGIC %md` for markdown cells.

## Local checks before a PR

```bash
# Validate the bundle for both targets
databricks bundle validate -t dev
databricks bundle validate -t prod

# Byte-compile the app
python -m py_compile src/app/app.py
```

## Making changes

| Change | Where |
| --- | --- |
| New config knob | `databricks.yml` variables → `resources/*.yml` → widget/env |
| App behavior | `src/app/app.py` |
| Data sync logic | `src/notebooks/sync_nba_to_lakebase.py` |
| Reconciliation | `src/notebooks/reconcile_action_catalog.py` |
| Model | `src/notebooks/nba_model_training_and_deploy.py` |
| New job | `resources/nba_jobs.yml` |

## Commit style

Small, focused commits with a clear subject line. Describe *why*, not just what.
