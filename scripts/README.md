# Demo & setup scripts

One-command helpers so you never have to remember the install/reset/demo steps.
**Everything is resolved from `databricks.yml`** (`databricks bundle summary`), so
these work in *any* workspace once you've set the target's host + variables —
nothing is hardcoded to a specific workspace.

## Prerequisites

- Databricks CLI v0.239.0+, authenticated (`databricks auth login` or a profile)
- `python3` with `databricks-sdk` + `psycopg2-binary` (the scripts use them for
  Lakebase access): `pip install "databricks-sdk>=0.118.0" psycopg2-binary`
- A `databricks.yml` (copy from `databricks.yml.template`) with your target's
  `workspace.host` + variables set

> Pass the **target** as the first arg to every script (default `dev`), e.g.
> `./scripts/setup.sh azure`.

## The three scripts

| Script | What it does | When |
| --- | --- | --- |
| `setup.sh <target>` | Full install: deploy → seed → train → bootstrap → deploy → bootstrap → start app. **CDF is enabled automatically** (via the Lakebase CDF API); the script then verifies it's streaming. | First time in a new/empty workspace, or full recovery. ~15–20 min, **zero manual steps**. |
| `demo.sh <target>` | The reconcile demo: adds a sample action on app-writes → waits for CDF → runs reconcile → verifies it's live on production. | In front of an audience, to show the CDF publish loop. ~2–3 min. |
| `reset.sh <target>` | Baseline reset (non-destructive): actions back to 16 baseline, branch + CDF untouched. Add `--full` for a destructive rebuild (re-enables CDF automatically). | Someone dirtied the demo env and you need a clean slate fast. |
| `destroy.sh <target>` | **Full teardown**: removes the app + jobs, **purges the Lakebase project** (hard delete, name reusable now), and **drops the UC schemas**. Flags: `--drop-catalog` (drop the whole catalog), `--keep-catalog`, `--keep-project`, `--yes`. | You want to wipe the environment (then reinstall with `setup.sh`). |

## Typical flow

```bash
# 1. First time in a workspace (or after a wipe) — fully hands-off, incl. CDF
./scripts/setup.sh azure

# 2. Run the demo (repeatable — unique action id each time)
./scripts/demo.sh azure

# 3. If the env got messy before a demo, quick clean slate:
./scripts/reset.sh azure          # keeps CDF running, no manual step
#    (only if truly broken:)  ./scripts/reset.sh azure --full

# 4. Clean up demo rows afterward
./scripts/demo.sh azure --cleanup && databricks bundle run nba_reconcile -t azure

# 5. Tear it all down (app + jobs + Lakebase project), then reinstall clean
./scripts/destroy.sh azure        # purges the project so the name is reusable now
./scripts/setup.sh   azure        # fresh install from scratch
```

## Teardown details

`destroy.sh` cleans the workspace in three steps: removes the **app + jobs**
(`bundle destroy`), **purges the Lakebase project** so its name is immediately
reusable, and **drops the Unity Catalog data**. It handles the gotchas for you:
- The project resource has `prevent_destroy: true` (a guard against accidental
  deletion). The script temporarily lifts it for the destroy, then restores the
  file unchanged.
- `bundle destroy` / the CLI only **soft-delete** a project (7-day retention,
  blocks reusing the name). The script instead **hard-deletes** it via the REST
  API (`DELETE /api/2.0/postgres/projects/<id>?purge=true`).
- UC cleanup uses a SQL warehouse it auto-finds; by default it drops the two
  project schemas (`<uc_catalog>.<uc_schema>` and `<cdf_catalog>.<cdf_schema>`).

Flags:
- `--drop-catalog` — drop the entire `<uc_catalog>` instead of just our schemas
- `--keep-catalog` — leave UC data untouched
- `--keep-project` — leave the Lakebase project in place
- `--yes` — skip the confirmation prompt

> Timing: the teardown itself is ~1–2 min. Reinstalling with `setup.sh` takes
> ~15 min — almost entirely model training (`nba_train_model`).

## CDF enablement (now automated)

Enabling Lakebase **Change Data Feed** on the app-writes branch is done
**automatically** by `nba_bootstrap` via the Lakebase CDF API — `setup.sh` and
`reset.sh --full` handle it end-to-end and then verify the feed is streaming, so
there is **no manual step**. CDF is (re-)enabled only when the app-writes branch
is (re)created; a normal `reset.sh` keeps it running.

If the CDF API is unavailable in a given workspace (older release), the bootstrap
notebook and these scripts fall back to printing the one-time manual UI click-path
so the install still completes. See the repo `README.md` and `SETUP.md` for the
full lifecycle.
