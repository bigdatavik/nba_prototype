# Databricks notebook source
# DBTITLE 1,Architecture Diagram
# MAGIC %md
# MAGIC ## 🖼️ NBA Prototype — End-to-End Architecture (Lakehouse + Lakebase)
# MAGIC
# MAGIC ![NBA end-to-end architecture](../../images/nba_end_to_end_architecture.png "NBA Prototype — Lakehouse + Lakebase end-to-end architecture")
# MAGIC


# COMMAND ----------

# DBTITLE 1,Notebook Overview
# MAGIC %md
# MAGIC ## 📚 Notebook Overview: `sync_nba_to_lakebase` (CDF edition)
# MAGIC
# MAGIC **Purpose:** Sync member features (and optionally the action catalog) from Unity
# MAGIC Catalog to the Lakebase **production branch** for sub-5ms reads by the NBA app,
# MAGIC and stand up the **app-writes** branch + **Change Data Feed (CDF)** plumbing that
# MAGIC `reconcile_action_catalog` consumes.
# MAGIC
# MAGIC ### Two ways to run this notebook
# MAGIC
# MAGIC | Mode | Parameters | What it does | When |
# MAGIC | --- | --- | --- | --- |
# MAGIC | **Regular sync** | `reset_environment=false`, `refresh_action_catalog=false` | Refreshes `member_features` only. Leaves `action_catalog` + the app-writes branch + CDF **untouched**. | Daily / every normal run (`nba_daily_sync`). Pair with `reconcile_action_catalog` for the complete edit→publish loop. |
# MAGIC | **Reset / rebuild** | `reset_environment=true` | **Destructive & rare.** Deletes the app-writes branch, drops prod tables, rebuilds everything, re-forks a fresh app-writes branch, and **re-enables CDF automatically via the API.** | Only when you deliberately rebuild the branch. |
# MAGIC
# MAGIC > **The app-writes branch is permanent.** Unlike the old trigger design, a normal
# MAGIC > run does **not** re-fork it. You create it once (first bootstrap); CDF is enabled
# MAGIC > automatically via the Lakebase CDF API, and from then on the regular loop (this
# MAGIC > notebook + reconcile) runs untouched. A reset is the *only* thing that recreates
# MAGIC > the branch, and it re-enables CDF automatically (with a manual-UI fallback if the
# MAGIC > API is unavailable).
# MAGIC
# MAGIC ### The complete regular loop (no reset)
# MAGIC ```text
# MAGIC   nba_daily_sync   → sync_nba_to_lakebase  (member_features → production)
# MAGIC   nba_reconcile    → reconcile_action_catalog (CDF app edits → UC → production)
# MAGIC ```
# MAGIC Business user edits an action in the app → change is captured by CDF into the UC
# MAGIC history table → `nba_reconcile` MERGEs it to UC and re-syncs production. No branch
# MAGIC re-fork, no manual step.
# MAGIC
# MAGIC **Key design decisions:**
# MAGIC 1. **Dynamic host resolution** — `w.postgres.get_endpoint()` resolves the current production hostname. Never hardcoded.
# MAGIC 2. **SDK authentication** — `WorkspaceClient()` works on serverless (no compute identity issues).
# MAGIC 3. **Idempotent branch creation** — the app-writes branch is created only if missing; a normal run never re-forks it.
# MAGIC 4. **REPLICA IDENTITY FULL is automatic** — set on `action_catalog` at table creation (persistent; the fork inherits it). Never a manual SQL step.
# MAGIC 5. **SP grants after every sync** — after table recreation the SP loses access; this notebook re-grants automatically.
# MAGIC
# MAGIC **Dependencies:**
# MAGIC - `databricks-sdk >= 0.118.0` (for `w.postgres.*` API)
# MAGIC - `psycopg2-binary` (for direct Postgres connections)
# MAGIC - Tables: `<uc_catalog>.<uc_schema>.serving_member_features`, `<uc_catalog>.<uc_schema>.action_catalog`


# COMMAND ----------

# DBTITLE 1,Install psycopg2 and upgrade SDK
%pip install psycopg2-binary "databricks-sdk>=0.118.0" --quiet
dbutils.library.restartPython()


# COMMAND ----------

# DBTITLE 1,Step 1 Explanation
# MAGIC %md
# MAGIC ### ⚙️ Cell 2: Configuration
# MAGIC
# MAGIC Sets up source (Unity Catalog) and target (Lakebase) coordinates. The `TABLES_TO_SYNC` list is built dynamically based on the `refresh_action_catalog` parameter:
# MAGIC - `false` (default): Only syncs `member_features` — safe for daily runs, won't overwrite business edits to action catalog
# MAGIC - `true`: Also syncs `action_catalog` from UC — use only for initial bootstrap or intentional full reset
# MAGIC
# MAGIC **Why this matters:** The action catalog is collaboratively edited by clinical ops via the app. Overwriting it with the UC snapshot would destroy pending CRUD changes.


# COMMAND ----------

# DBTITLE 1,Step 2 Explanation
# MAGIC %md
# MAGIC ### 🔐 Cell 3: Authenticate & Connect to Lakebase
# MAGIC
# MAGIC Uses the **Databricks SDK** (`WorkspaceClient`) to:
# MAGIC 1. Authenticate as the notebook user (works on serverless — no compute identity issues)
# MAGIC 2. **Dynamically resolve** the production branch hostname via `get_endpoint()` (never hardcoded)
# MAGIC 3. Generate a short-lived OAuth credential for Postgres via `generate_database_credential()`
# MAGIC 4. Open a psycopg2 connection to the production branch
# MAGIC
# MAGIC **Why not `ctx.apiToken()`?** On serverless compute, `apiToken()` returns the compute identity (spark-xxx), which isn't a Lakebase user. The SDK properly authenticates as you.


# COMMAND ----------

# DBTITLE 1,Step 3 Explanation
# MAGIC %md
# MAGIC ### 💣 Cell 4: Reset Environment (Conditional)
# MAGIC
# MAGIC Only runs when `reset_environment=true`. Performs a full wipe:
# MAGIC 1. **Deletes app-writes branch** (if exists) — removes the staging branch entirely
# MAGIC 2. **Drops all tables** in the production schema — clean slate
# MAGIC 3. **Forces both tables** into `TABLES_TO_SYNC` regardless of the other parameter
# MAGIC
# MAGIC After this cell, the remaining cells rebuild everything from scratch, and the
# MAGIC **final cell re-enables CDF automatically via the API** on the freshly re-forked
# MAGIC app-writes branch (CDF does not survive a re-fork). Use for demos or when the
# MAGIC environment is in a bad state.
# MAGIC
# MAGIC ⚠️ **This is destructive and rare!** All CRUD edits, branch state, and the CDF
# MAGIC history table are lost, and a human must re-enable CDF afterward. Normal
# MAGIC operation never touches this path.


# COMMAND ----------

# DBTITLE 1,Step 4 Explanation
# MAGIC %md
# MAGIC ### 🔧 Cell 5: Sync Function Definition
# MAGIC
# MAGIC Defines `sync_table()` which:
# MAGIC 1. Reads a UC table into memory (Spark `.collect()`)
# MAGIC 2. Maps Spark types → Postgres types (string→TEXT, array→JSONB, etc.)
# MAGIC 3. Creates the Postgres table if it doesn't exist (`CREATE TABLE IF NOT EXISTS`)
# MAGIC 4. Truncates existing data (`TRUNCATE TABLE`)
# MAGIC 5. Bulk-inserts all rows using `execute_values()` (fast batch insert)
# MAGIC 6. Verifies row count matches source
# MAGIC
# MAGIC **Note:** This is a full-replace pattern (not incremental). For 50 member rows and 16 action rows, this is perfectly efficient.


# COMMAND ----------

# DBTITLE 1,Step 5 Explanation
# MAGIC %md
# MAGIC ### ▶️ Cell 6: Execute Sync
# MAGIC
# MAGIC Creates the schema if needed, then iterates through `TABLES_TO_SYNC` and calls `sync_table()` for each.
# MAGIC
# MAGIC **Output:** A summary showing row counts for each synced table. Expected:
# MAGIC - `member_features`: 50 rows (always)
# MAGIC - `action_catalog`: 16 rows (only if `refresh_action_catalog=true` or `reset_environment=true`)


# COMMAND ----------

# DBTITLE 1,Step 6 Explanation
# MAGIC %md
# MAGIC ### ✅ Cell 7: Verify + REPLICA IDENTITY + Grant SP on Production
# MAGIC
# MAGIC Does four things:
# MAGIC 1. **Verifies** data landed correctly (row counts + sample rows)
# MAGIC 2. **Creates indexes** for fast point lookups (`member_id`, `action_id`)
# MAGIC 3. **Sets `REPLICA IDENTITY FULL`** on `action_catalog` (automatic, idempotent) so
# MAGIC    the CDF feed carries the full old-row image on UPDATE/DELETE. Set on the
# MAGIC    production branch; the app-writes fork inherits it copy-on-write. **You never
# MAGIC    type this SQL by hand.**
# MAGIC 4. **Grants the app's Service Principal** (nba-console) `USAGE + SELECT` on the production schema
# MAGIC
# MAGIC **Why grant here?** When tables are dropped and recreated (during reset or first run), the new tables are owned by the notebook user. The SP needs explicit `SELECT` to read them. This is a separate transaction from `CREATE ROLE` so it always succeeds even if the role already exists.


# COMMAND ----------

# DBTITLE 1,Step 7 Explanation
# MAGIC %md
# MAGIC ### 🌿 Cell 8: Ensure App-Writes Branch Exists (permanent)
# MAGIC
# MAGIC This cell is **idempotent** — it only creates the app-writes branch if it doesn't
# MAGIC already exist. **A normal run never re-forks it** (the branch is permanent under
# MAGIC the CDF design); only a `reset_environment=true` run recreates it.
# MAGIC
# MAGIC **First run (branch missing):**
# MAGIC 1. Forks app-writes from production (copy-on-write, instant, zero storage cost).
# MAGIC 2. Grants the nba-console SP full access (USAGE + ALL TABLES + SEQUENCES).
# MAGIC 3. **`REPLICA IDENTITY FULL` is inherited** from production (set in Cell 7), so the
# MAGIC    fork is already CDF-ready — no per-branch SQL needed.
# MAGIC
# MAGIC > A legacy audit trigger + `_action_catalog_changes` table are still created here
# MAGIC > for parallel-run validation against the old design. They are **not** used by the
# MAGIC > CDF reconcile and will be removed once cutover is complete.
# MAGIC
# MAGIC **Subsequent runs (branch exists):** prints "already exists" and skips. No re-fork.
# MAGIC
# MAGIC **After a fresh fork, CDF is enabled automatically** by the final cell via the
# MAGIC Lakebase CDF API (no manual step). If that API is unavailable in a given
# MAGIC workspace, the cell falls back to printing the one-time manual UI step.
# MAGIC
# MAGIC **Why a branch?** Copy-on-write isolation — business users edit actions on this branch via the app. Their changes don't affect production scoring until reconciled.


# COMMAND ----------

# DBTITLE 1,Configuration
# =====================================================================
# Configuration — all values are read from notebook widgets/job params.
# The Databricks Asset Bundle passes these per-target (see resources/
# nba_jobs.yml), so NOTHING here is hardcoded to a workspace, catalog,
# Lakebase project, or endpoint host. Defaults below are dev-friendly
# and can be overridden without editing this notebook.
# =====================================================================

# Declare widgets (idempotent — safe to re-run). Job base_parameters and
# the DABs `parameters` block override these defaults at runtime.
dbutils.widgets.text("uc_catalog", "nba_demo", "UC catalog")
dbutils.widgets.text("uc_schema", "nba_new", "UC schema")
dbutils.widgets.text("lakebase_database", "databricks_postgres", "Lakebase database (SQL dbname)")
dbutils.widgets.text("lakebase_database_id", "databricks-postgres", "Lakebase database RESOURCE id (hyphenated)")
dbutils.widgets.text("lakebase_schema", "nba_new_lbase", "Lakebase (Postgres) schema")
dbutils.widgets.text("lakebase_project", "lakebase-demo-autoscale", "Lakebase project id")
dbutils.widgets.text("lakebase_branch", "production", "Lakebase target branch")
dbutils.widgets.text("app_writes_branch", "app-writes", "Lakebase app-writes branch")
dbutils.widgets.text("nba_console_sp", "", "App service principal client id")
dbutils.widgets.dropdown("reset_environment", "false", ["true", "false"], "Reset entire demo (DESTRUCTIVE)")
dbutils.widgets.dropdown("refresh_action_catalog", "false", ["true", "false"], "Refresh action catalog (bootstrap)")

# Source: Unity Catalog
UC_CATALOG = dbutils.widgets.get("uc_catalog")
UC_SCHEMA = dbutils.widgets.get("uc_schema")

# Target: Lakebase Autoscaling
LAKEBASE_DATABASE = dbutils.widgets.get("lakebase_database")
# Database RESOURCE id (used in Lakebase REST resource paths). This is usually
# the hyphenated form of the SQL dbname, e.g. "databricks-postgres" for the
# "databricks_postgres" database. Used by the CDF-config API path.
LAKEBASE_DATABASE_ID = dbutils.widgets.get("lakebase_database_id")
LAKEBASE_SCHEMA = dbutils.widgets.get("lakebase_schema")
LAKEBASE_PROJECT = dbutils.widgets.get("lakebase_project")
LAKEBASE_BRANCH = dbutils.widgets.get("lakebase_branch")
APP_WRITES_BRANCH_ID = dbutils.widgets.get("app_writes_branch")
LAKEBASE_ENDPOINT = f"projects/{LAKEBASE_PROJECT}/branches/{LAKEBASE_BRANCH}/endpoints/primary"
# Host is resolved dynamically from the SDK in the next cell (never hardcoded)
LAKEBASE_HOST = None

# --- Lakebase CDF (Change Data Feed) coordinates ---
# CDF replaces the Postgres trigger + change-log table for reconciliation.
# The destination history table + watermark live in a dedicated UC schema.
# Enabling CDF is now AUTOMATED via the Lakebase CDF API (see the final cell);
# if that API is unavailable in a given workspace, the notebook falls back to
# printing the one-time manual UI step.
dbutils.widgets.text("cdf_catalog", "nba_demo", "CDF catalog (history + watermark)")
dbutils.widgets.text("cdf_schema", "cdf", "CDF schema (history + watermark)")
CDF_CATALOG = dbutils.widgets.get("cdf_catalog")
CDF_SCHEMA = dbutils.widgets.get("cdf_schema")
# Lakebase CDF names the destination table lb_<pg_table>_history (the UI's fixed
# convention). This must match what the "Start Lakebase CDF" UI creates for
# nba_new_lbase.action_catalog, so the reset path drops the right table.
CDF_HISTORY_TABLE = f"{CDF_CATALOG}.{CDF_SCHEMA}.lb_action_catalog_history"
CDF_WATERMARK_TABLE = f"{CDF_CATALOG}.{CDF_SCHEMA}.action_catalog_watermark"
UC_ACTION_CATALOG = f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog"
# Decisions (Approve & Act) — governed UC net-state + its CDF history table.
# Reconciled by reconcile_nba_decisions (CDF → UC); no production re-sync.
UC_DECISIONS = f"{UC_CATALOG}.{UC_SCHEMA}.nba_decisions"
CDF_DECISIONS_HISTORY = f"{CDF_CATALOG}.{CDF_SCHEMA}.lb_nba_decisions_history"

# Tables to sync
# member_features: always synced (daily refresh from feature pipeline)
# action_catalog: ONLY synced if refresh_action_catalog = true (initial bootstrap)
#   Otherwise, action_catalog is managed by reconcile_action_catalog notebook
#   (CRUD → UC → production flow). Syncing it here would overwrite business edits.
TABLES_TO_SYNC = [
    {
        "source": f"{UC_CATALOG}.{UC_SCHEMA}.serving_member_features",
        "target": "member_features",
        "primary_key": "member_id",
    },
]

# Original action_ids (baseline 16 rows) — any rows beyond these are app-added
# and should NOT be synced when refreshing to baseline state
ORIGINAL_ACTION_IDS = [
    'ACT001', 'ACT002', 'ACT003', 'ACT004', 'ACT005', 'ACT006',
    'ACT007', 'ACT008', 'ACT009', 'ACT010', 'ACT011', 'ACT012',
    'ACT014', 'ACT015', 'ACT016', 'ACT017',
]

# Conditionally add action_catalog (bootstrap / initial setup only)
if dbutils.widgets.get("refresh_action_catalog") == "true":
    TABLES_TO_SYNC.append({
        "source": f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog",
        "target": "action_catalog",
        "primary_key": "action_id",
        "filter_ids": ORIGINAL_ACTION_IDS,  # Only sync original rows
    })
    print("⚠️  refresh_action_catalog=true → action_catalog WILL be refreshed with ORIGINAL 16 rows only")
    print(f"   (app-added rows will be excluded from sync)")
else:
    print("ℹ️  refresh_action_catalog=false → action_catalog untouched (managed by reconciliation)")

print(f"Source: {UC_CATALOG}.{UC_SCHEMA}")
print(f"Target: {LAKEBASE_HOST} / {LAKEBASE_DATABASE} / {LAKEBASE_SCHEMA}")
print(f"Tables: {[t['target'] for t in TABLES_TO_SYNC]}")


# COMMAND ----------

# DBTITLE 1,Generate Lakebase credential and connect
import psycopg2
from psycopg2.extras import execute_values
from databricks.sdk import WorkspaceClient

# SDK authenticates as the notebook user (works on serverless)
w = WorkspaceClient()
db_user = w.current_user.me().user_name
print(f"Authenticated as: {db_user}")

# Resolve the app service principal that needs Postgres grants.
# Priority: explicit widget value → auto-discover from the deployed app →
# skip grants (with a warning) so the notebook still completes.
dbutils.widgets.text("nba_console_app_name", "", "App name (for SP auto-resolve)")
NBA_CONSOLE_SP = dbutils.widgets.get("nba_console_sp").strip()
if not NBA_CONSOLE_SP:
    try:
        # Auto-discover: find the app and read its service principal client id
        app_name = dbutils.widgets.get("nba_console_app_name").strip()
        for _app in w.apps.list():
            if (app_name and _app.name == app_name) or (not app_name and "nba" in (_app.name or "").lower()):
                NBA_CONSOLE_SP = _app.service_principal_client_id or ""
                print(f"✅ Auto-resolved app SP from app '{_app.name}': {NBA_CONSOLE_SP}")
                break
    except Exception as _e:
        print(f"⚠️  Could not auto-resolve app SP: {_e}")
if NBA_CONSOLE_SP:
    print(f"App SP for grants: {NBA_CONSOLE_SP}")
else:
    print("⚠️  No app SP configured — Postgres GRANT steps will be skipped. "
          "Set the 'nba_console_sp' widget or deploy the app first.")

# Resolve production host dynamically (survives if host ever changes)
ep = w.postgres.get_endpoint(name=LAKEBASE_ENDPOINT)
LAKEBASE_HOST = ep.status.hosts.host
print(f"\u2705 Resolved host: {LAKEBASE_HOST}")

# Generate Lakebase credential using SDK
db_token = w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT).token
print(f"\u2705 Got credential for production branch")

# Connect to Lakebase
conn = psycopg2.connect(
    host=LAKEBASE_HOST,
    port=5432,
    dbname=LAKEBASE_DATABASE,
    user=db_user,
    password=db_token,
    sslmode="require"
)
conn.autocommit = False
print(f"\u2705 Connected to Lakebase: {LAKEBASE_HOST}")


# COMMAND ----------

# DBTITLE 1,Reset environment (if reset_environment=true)
# If reset_environment=true, wipe everything clean and start fresh
if dbutils.widgets.get("reset_environment") == "true":
    print("\u26a0\ufe0f  RESET MODE: Wiping Lakebase environment clean...")
    print("="*60)

    # Step 1: Delete app-writes branch (if exists)
    print(f"\n[1/3] Deleting {APP_WRITES_BRANCH_ID} branch...")
    branch_deleted = False
    for b in w.postgres.list_branches(parent=f"projects/{LAKEBASE_PROJECT}"):
        if APP_WRITES_BRANCH_ID in b.name:
            w.postgres.delete_branch(name=b.name)
            print(f"   \u2705 Deleted: {b.name}")
            branch_deleted = True
            break
    if not branch_deleted:
        print("   \u2139\ufe0f  app-writes branch not found (already clean)")

    # Step 2: Drop all tables in production schema
    print("\n[2/3] Dropping all tables in production schema...")
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT tablename FROM pg_tables 
            WHERE schemaname = '{LAKEBASE_SCHEMA}'
        """)
        tables = [row[0] for row in cur.fetchall()]
        for table in tables:
            cur.execute(f'DROP TABLE IF EXISTS {LAKEBASE_SCHEMA}."{table}" CASCADE')
            print(f"   Dropped: {LAKEBASE_SCHEMA}.{table}")
    conn.commit()
    if tables:
        print(f"   \u2705 Dropped {len(tables)} tables")
    else:
        print("   \u2139\ufe0f  Schema already empty")

    # Step 3: Force both tables to be synced (with filter to original rows only)
    print("\n[3/3] Forcing full sync (member_features + action_catalog)...")
    TABLES_TO_SYNC.clear()
    TABLES_TO_SYNC.extend([
        {
            "source": f"{UC_CATALOG}.{UC_SCHEMA}.serving_member_features",
            "target": "member_features",
            "primary_key": "member_id",
        },
        {
            "source": f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog",
            "target": "action_catalog",
            "primary_key": "action_id",
            "filter_ids": ORIGINAL_ACTION_IDS,  # Only sync original rows
        },
    ])
    print(f"   Tables to sync: {[t['target'] for t in TABLES_TO_SYNC]}")
    print(f"   action_catalog filtered to {len(ORIGINAL_ACTION_IDS)} original rows (app-added excluded)")
    print(f"\n{'='*60}")
    print("\u2705 Environment wiped. Remaining cells will rebuild everything.")
    print("   - Cell 5: Re-creates tables + inserts data")
    print("   - Cell 7: Re-creates app-writes branch + audit trigger + SP grants")
else:
    print("\u2139\ufe0f  reset_environment=false \u2014 running normal sync")


# COMMAND ----------

# DBTITLE 1,Sync function - create table and insert data
def get_pg_type(spark_type: str) -> str:
    """Map Spark types to PostgreSQL types."""
    type_map = {
        'string': 'TEXT',
        'int': 'INTEGER',
        'integer': 'INTEGER',
        'bigint': 'BIGINT',
        'long': 'BIGINT',
        'double': 'DOUBLE PRECISION',
        'float': 'REAL',
        'boolean': 'BOOLEAN',
        'date': 'DATE',
        'timestamp': 'TIMESTAMP WITH TIME ZONE',
    }
    spark_type_lower = spark_type.lower()
    # Handle decimal/numeric
    if spark_type_lower.startswith('decimal'):
        return 'NUMERIC'
    # Handle array types
    if spark_type_lower.startswith('array'):
        return 'JSONB'
    return type_map.get(spark_type_lower, 'TEXT')


def sync_table(conn, source_table: str, target_table: str, primary_key: str, filter_ids: list = None):
    """Sync a UC table to Lakebase via full replace.
    
    Args:
        filter_ids: If provided, only sync rows where primary_key is in this list.
                    Used to exclude app-added rows and sync only original baseline data.
    """
    print(f"\n{'='*60}")
    print(f"Syncing: {source_table} -> {LAKEBASE_SCHEMA}.{target_table}")
    print(f"{'='*60}")
    
    # Read source data from UC
    df = spark.table(source_table)
    
    # Filter to original rows only (excludes app-added records)
    if filter_ids:
        from pyspark.sql.functions import col
        df = df.filter(col(primary_key).isin(filter_ids))
        print(f"  Filtering to {len(filter_ids)} original rows (excluding app-added)")
    
    schema = df.schema
    rows = df.collect()
    print(f"  Source rows: {len(rows)}")
    print(f"  Columns: {len(schema.fields)}")
    
    # Build CREATE TABLE DDL
    col_defs = []
    for field in schema.fields:
        pg_type = get_pg_type(field.dataType.simpleString())
        nullable = '' if field.nullable else ' NOT NULL'
        pk = ' PRIMARY KEY' if field.name == primary_key else ''
        col_defs.append(f'    "{field.name}" {pg_type}{nullable}{pk}')
    
    create_ddl = f"""CREATE TABLE IF NOT EXISTS {LAKEBASE_SCHEMA}.{target_table} (
{(',' + chr(10)).join(col_defs)}
)"""
    
    with conn.cursor() as cur:
        # Create table if not exists, truncate for clean sync
        cur.execute(create_ddl)
        cur.execute(f"TRUNCATE TABLE {LAKEBASE_SCHEMA}.{target_table}")
        
        # Insert data
        if rows:
            col_names = [f'"{f.name}"' for f in schema.fields]
            insert_sql = f"INSERT INTO {LAKEBASE_SCHEMA}.{target_table} ({', '.join(col_names)}) VALUES %s"
            
            # Convert Spark rows to tuples
            values = []
            for row in rows:
                vals = []
                for i, field in enumerate(schema.fields):
                    val = row[i]
                    # Convert arrays to JSON string for JSONB
                    if val is not None and field.dataType.simpleString().startswith('array'):
                        import json
                        vals.append(json.dumps(list(val)))
                    else:
                        vals.append(val)
                values.append(tuple(vals))
            
            execute_values(cur, insert_sql, values, page_size=100)
    
    conn.commit()
    
    # Verify
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {LAKEBASE_SCHEMA}.{target_table}")
        count = cur.fetchone()[0]
    
    print(f"  Target rows: {count}")
    assert count == len(rows), f"Row count mismatch! Source={len(rows)}, Target={count}"
    print(f"  ✅ Sync verified")
    return {"table": target_table, "rows": count, "success": True}

print("Sync function ready.")


# COMMAND ----------

# DBTITLE 1,Execute sync - create schema and sync tables
# Reset transaction state from any prior error
conn.rollback()

# Ensure schema exists
with conn.cursor() as cur:
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {LAKEBASE_SCHEMA}")
conn.commit()
print(f"Schema '{LAKEBASE_SCHEMA}' ready")

# Sync all tables
results = []
for table_config in TABLES_TO_SYNC:
    result = sync_table(
        conn=conn,
        source_table=table_config["source"],
        target_table=table_config["target"],
        primary_key=table_config["primary_key"],
        filter_ids=table_config.get("filter_ids"),
    )
    results.append(result)

# Summary
print(f"\n{'='*60}")
print(f"SYNC COMPLETE")
print(f"{'='*60}")
for r in results:
    print(f"  {LAKEBASE_SCHEMA}.{r['table']}: {r['rows']} rows ✅")


# COMMAND ----------

# DBTITLE 1,Verify - query Lakebase tables
# Phase 4b: Verify data landed in Lakebase
print("Verifying Lakebase tables...\n")

with conn.cursor() as cur:
    # Check member_features
    cur.execute(f"SELECT count(*) FROM {LAKEBASE_SCHEMA}.member_features")
    mf_count = cur.fetchone()[0]
    
    cur.execute(f"SELECT member_id, age, churn_risk_score, engagement_score FROM {LAKEBASE_SCHEMA}.member_features LIMIT 5")
    mf_sample = cur.fetchall()
    
    # Check action_catalog
    cur.execute(f"SELECT count(*) FROM {LAKEBASE_SCHEMA}.action_catalog")
    ac_count = cur.fetchone()[0]
    
    cur.execute(f"SELECT action_id, action_name, value_score FROM {LAKEBASE_SCHEMA}.action_catalog LIMIT 5")
    ac_sample = cur.fetchall()

print(f"{LAKEBASE_SCHEMA}.member_features: {mf_count} rows")
print(f"  Sample: {mf_sample[:3]}")
print(f"\n{LAKEBASE_SCHEMA}.action_catalog: {ac_count} rows")
print(f"  Sample: {ac_sample[:3]}")

# Create index on member_id for fast lookups
with conn.cursor() as cur:
    cur.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_member_features_member_id 
        ON {LAKEBASE_SCHEMA}.member_features (member_id)
    """)
    cur.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_action_catalog_action_id
        ON {LAKEBASE_SCHEMA}.action_catalog (action_id)
    """)
conn.commit()
print(f"\n✅ Indexes created for fast point lookups")

# --- Lakebase CDF prerequisite: REPLICA IDENTITY FULL (automated, idempotent) ---
# Makes UPDATE/DELETE changes in the CDF feed carry the full old-row image.
# Set here on the PRODUCTION branch so the app-writes fork inherits it
# (copy-on-write). This is a persistent table property — applied at creation,
# never a manual step. Safe to re-run.
with conn.cursor() as cur:
    cur.execute(f"ALTER TABLE {LAKEBASE_SCHEMA}.action_catalog REPLICA IDENTITY FULL")
conn.commit()
print(f"✅ REPLICA IDENTITY FULL set on {LAKEBASE_SCHEMA}.action_catalog (CDF-ready)")

# Grant SP read access on PRODUCTION branch (needed after table recreation).
# NBA_CONSOLE_SP was resolved (widget or app auto-discovery) in the connect cell.
if not NBA_CONSOLE_SP:
    print("\n⚠️  Skipping SP grants on PRODUCTION — no app SP configured.")
    print("   Set the 'nba_console_sp' widget or deploy the app, then re-run.")
else:
    print(f"\n{'='*60}")
    print(f"GRANTING SP ACCESS ON PRODUCTION BRANCH")
    print(f"{'='*60}")
    print(f"   SP: {NBA_CONSOLE_SP}")
    print(f"   Branch: {LAKEBASE_BRANCH}")
    print(f"   Host: {LAKEBASE_HOST}")

    # Step 1: Create role (separate transaction — may already exist)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE ROLE "{NBA_CONSOLE_SP}" LOGIN')
        conn.commit()
        print(f"   ✅ Created Postgres role for SP")
    except Exception:
        conn.rollback()
        print(f"   ℹ️  Role already exists (skipped create)")

    # Step 2: Grant privileges (separate transaction — always succeeds)
    with conn.cursor() as cur:
        cur.execute(f'GRANT USAGE ON SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
        cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
    conn.commit()
    print(f"   ✅ GRANT USAGE ON SCHEMA {LAKEBASE_SCHEMA} TO SP")
    print(f"   ✅ GRANT SELECT ON ALL TABLES IN SCHEMA {LAKEBASE_SCHEMA} TO SP")
    print(f"\n✅ SP can now read member_features + action_catalog from PRODUCTION")
    print(f"{'='*60}")

print(f"\n✅ Phase 4 complete — Lakebase ready for app consumption")

conn.close()


# COMMAND ----------

# DBTITLE 1,Ensure app-writes branch exists (create if missing)
# Ensure app-writes branch exists (idempotent — skips if already present)
from databricks.sdk.service.postgres import Branch, BranchSpec

APP_WRITES_BRANCH = f"projects/{LAKEBASE_PROJECT}/branches/{APP_WRITES_BRANCH_ID}"
PRODUCTION_BRANCH = f"projects/{LAKEBASE_PROJECT}/branches/{LAKEBASE_BRANCH}"

# Check if app-writes already exists
branch_exists = False
for b in w.postgres.list_branches(parent=f"projects/{LAKEBASE_PROJECT}"):
    if APP_WRITES_BRANCH_ID in b.name:
        branch_exists = True
        break

# Signals to the final cell whether CDF must be (re-)enabled by hand. A freshly
# forked branch has no CDF (it does not survive a fork), so the enable banner must
# print. The app-writes branch is PERMANENT; a normal run never re-forks it.
APP_WRITES_BRANCH_CREATED = not branch_exists

if branch_exists:
    print(f"\u2705 {APP_WRITES_BRANCH_ID} branch already exists — skipping creation")
    print("   (permanent branch; CDF stays enabled; reconcile advances the LSN watermark)")
else:
    print(f"Creating {APP_WRITES_BRANCH_ID} branch (fork from {LAKEBASE_BRANCH})...")
    op = w.postgres.create_branch(
        parent=f"projects/{LAKEBASE_PROJECT}",
        branch=Branch(spec=BranchSpec(
            source_branch=PRODUCTION_BRANCH,
            no_expiry=True,
        )),
        branch_id=APP_WRITES_BRANCH_ID,
    )
    new_branch = op.wait()
    print(f"\u2705 Created: {new_branch.name}")

    # Get new endpoint host
    import time
    time.sleep(5)  # Wait for endpoint to initialize
    for ep in w.postgres.list_endpoints(parent=APP_WRITES_BRANCH):
        new_host = ep.status.hosts.host
        new_endpoint = ep.name
        print(f"   Endpoint: {new_host}")

    # Connect and set up audit infrastructure
    token_new = w.postgres.generate_database_credential(endpoint=new_endpoint).token
    conn_new = psycopg2.connect(
        host=new_host, port=5432, dbname=LAKEBASE_DATABASE,
        user=db_user, password=token_new, sslmode="require"
    )
    conn_new.autocommit = True

    with conn_new.cursor() as cur:
        # Create audit table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {LAKEBASE_SCHEMA}._action_catalog_changes (
                change_id SERIAL PRIMARY KEY,
                operation TEXT NOT NULL,
                action_id TEXT NOT NULL,
                old_values JSONB,
                new_values JSONB,
                changed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        # Create trigger function
        cur.execute(f"""
            CREATE OR REPLACE FUNCTION {LAKEBASE_SCHEMA}.log_action_catalog_changes()
            RETURNS TRIGGER AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    INSERT INTO {LAKEBASE_SCHEMA}._action_catalog_changes (operation, action_id, new_values)
                    VALUES ('INSERT', NEW.action_id, to_jsonb(NEW));
                ELSIF TG_OP = 'UPDATE' THEN
                    INSERT INTO {LAKEBASE_SCHEMA}._action_catalog_changes (operation, action_id, old_values, new_values)
                    VALUES ('UPDATE', NEW.action_id, to_jsonb(OLD), to_jsonb(NEW));
                ELSIF TG_OP = 'DELETE' THEN
                    INSERT INTO {LAKEBASE_SCHEMA}._action_catalog_changes (operation, action_id, old_values)
                    VALUES ('DELETE', OLD.action_id, to_jsonb(OLD));
                END IF;
                RETURN COALESCE(NEW, OLD);
            END;
            $$ LANGUAGE plpgsql
        """)

        # Attach trigger
        cur.execute(f"DROP TRIGGER IF EXISTS trg_action_catalog_audit ON {LAKEBASE_SCHEMA}.action_catalog")
        cur.execute(f"""
            CREATE TRIGGER trg_action_catalog_audit
            AFTER INSERT OR UPDATE OR DELETE ON {LAKEBASE_SCHEMA}.action_catalog
            FOR EACH ROW EXECUTE FUNCTION {LAKEBASE_SCHEMA}.log_action_catalog_changes()
        """)

        # Grant SP permissions (guarded \u2014 the app SP may not exist yet on a
        # first install; re-run this job after the app is deployed to grant).
        if not NBA_CONSOLE_SP:
            print("   \u26a0\ufe0f  No app SP configured \u2014 skipping app-writes grants. "
                  "Deploy the app, then re-run this job to grant.")
        else:
            try:
                cur.execute(f'CREATE ROLE "{NBA_CONSOLE_SP}" LOGIN')
            except Exception:
                pass  # Role may already exist
            cur.execute(f'GRANT USAGE ON SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
            # CREATE lets the app own its own operational tables on app-writes
            # (e.g. nba_decisions for the Approve & Act loop) \u2014 writable Postgres,
            # never the read-only synced tables.
            cur.execute(f'GRANT CREATE ON SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
            cur.execute(f'GRANT ALL ON ALL TABLES IN SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
            cur.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')

    conn_new.close()
    print("\u2705 Audit trigger configured on app-writes")
    print("\n\U0001f389 First-time setup complete!")

# ---------------------------------------------------------------------------
# Always re-apply SP grants on the app-writes branch (idempotent), so a NORMAL
# run keeps the app's access current \u2014 including CREATE, so the app can own its
# operational tables (nba_decisions). The fork-time block above only runs when
# the branch is first created; this makes the notebook's "re-grant after every
# sync" promise true for app-writes too, without a destructive re-fork.
# ---------------------------------------------------------------------------
if NBA_CONSOLE_SP:
    try:
        _aw_endpoint = None
        _aw_host = None
        for _ep in w.postgres.list_endpoints(parent=APP_WRITES_BRANCH):
            _aw_endpoint = _ep.name
            _aw_host = _ep.status.hosts.host
        _aw_token = w.postgres.generate_database_credential(endpoint=_aw_endpoint).token
        _aw_conn = psycopg2.connect(
            host=_aw_host, port=5432, dbname=LAKEBASE_DATABASE,
            user=db_user, password=_aw_token, sslmode="require",
        )
        _aw_conn.autocommit = True
        with _aw_conn.cursor() as cur:
            try:
                cur.execute(f'CREATE ROLE "{NBA_CONSOLE_SP}" LOGIN')
            except Exception:
                pass  # role may already exist
            # Pre-create the Approve & Act decision log so it exists from install
            # (deterministic); the app also creates it lazily as a fallback. This
            # is operational app state on the writable app-writes branch.
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {LAKEBASE_SCHEMA}.nba_decisions (
                    decision_id       BIGSERIAL PRIMARY KEY,
                    member_id         TEXT NOT NULL,
                    action_id         TEXT,
                    action_name       TEXT,
                    channel           TEXT,
                    recommended_score DOUBLE PRECISION,
                    status            TEXT,
                    disposition       TEXT,
                    outcome           TEXT,
                    note              TEXT,
                    approver          TEXT,
                    created_at        TIMESTAMPTZ DEFAULT now()
                )
            """)
            # REPLICA IDENTITY FULL makes Lakebase CDF capture nba_decisions
            # (same requirement as action_catalog) and emit full before-images on
            # UPDATE (e.g. when an outcome is recorded) → the decision log streams
            # to UC as <cdf_catalog>.<cdf_schema>.lb_nba_decisions_history. Only the
            # table owner can set it; on app-created installs the app sets it itself.
            try:
                cur.execute(f'ALTER TABLE {LAKEBASE_SCHEMA}.nba_decisions REPLICA IDENTITY FULL')
            except Exception:
                pass
            cur.execute(f'GRANT USAGE ON SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
            cur.execute(f'GRANT CREATE ON SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
            cur.execute(f'GRANT ALL ON ALL TABLES IN SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
            cur.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA {LAKEBASE_SCHEMA} TO "{NBA_CONSOLE_SP}"')
        _aw_conn.close()
        print("\u2705 app-writes: ensured nba_decisions + re-applied SP grants "
              "(USAGE, CREATE, ALL TABLES, SEQUENCES)")
    except Exception as _e:
        print(f"\u26a0\ufe0f  Could not re-apply app-writes SP grants: {_e}")


# COMMAND ----------

# DBTITLE 1,Cell 9 - CDF destination schema, watermark, and reset handling
# ---------------------------------------------------------------------------
# This cell sets up the Unity Catalog SIDE of Change Data Feed (CDF):
#
#   (1) creates the dedicated UC schema that holds the CDF outputs, plus a tiny
#       watermark table the reconcile job uses to track its last-processed LSN.
#       Both are idempotent (safe to run on every job execution).
#   (2) on a RESET run (reset_environment=true, i.e. the app-writes branch was
#       just re-forked): drops the stale destination history table and resets
#       the watermark to -1 so reconcile reprocesses the fresh feed from the start.
#
# Uses only Spark SQL against Unity Catalog (no Postgres connection needed). The
# ACTUAL turning-on of CDF on the Postgres branch happens in the NEXT cell, via
# the Lakebase CDF API.
#
# Background: REPLICA IDENTITY FULL was already applied to action_catalog in the
# verify cell (Cell 7). It is a persistent table property, and because app-writes
# is a copy-on-write fork of production, the fork inherits it automatically. That
# property is what lets CDF emit full before-images on UPDATE/DELETE.
# ---------------------------------------------------------------------------

_RESET = dbutils.widgets.get("reset_environment") == "true"
# CDF has to be turned on whenever the app-writes branch is FRESH: a reset just
# re-forked it, or this was the first-ever run that created it. A CDF feed does
# not survive a branch re-fork, so a freshly forked branch always needs it
# (re-)enabled. APP_WRITES_BRANCH_CREATED is set in the branch-creation cell above.
_NEEDS_CDF_ENABLE = _RESET or bool(globals().get("APP_WRITES_BRANCH_CREATED", False))

# 1) Dedicated CDF schema + watermark table (idempotent, safe every run)
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CDF_CATALOG}.{CDF_SCHEMA} "
          f"COMMENT 'Lakebase CDF destination history + watermark for action_catalog + nba_decisions'")
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {CDF_WATERMARK_TABLE} (
        table_name STRING,
        last_lsn   BIGINT,
        updated_at TIMESTAMP
    )
""")
# Governed UC net-state for the Approve & Act decision log (populated by
# reconcile_nba_decisions from the CDF history). Pre-created so it exists on
# every rebuild and can be referenced by Genie / dashboards immediately.
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {UC_DECISIONS} (
        decision_id       BIGINT,
        member_id         STRING,
        action_id         STRING,
        action_name       STRING,
        channel           STRING,
        recommended_score DOUBLE,
        status            STRING,
        disposition       STRING,
        outcome           STRING,
        note              STRING,
        approver          STRING,
        created_at        TIMESTAMP
    ) USING DELTA
    COMMENT 'Governed net-state NBA decision log (reconciled from Lakebase CDF). Learning half of the Approve & Act closed loop.'
""")
print(f"\u2705 CDF schema ready: {CDF_CATALOG}.{CDF_SCHEMA}")
print(f"   watermark: {CDF_WATERMARK_TABLE}")
print(f"   governed decisions table: {UC_DECISIONS}")

if _RESET:
    # 2) Reset path \u2014 the branch was just re-forked, so the CDF feed is being
    #    restarted. Drop the stale destination history table and reset the
    #    watermark so the consumer reprocesses the fresh feed from the start.
    print("\n" + "=" * 60)
    print("RESET MODE \u2014 preparing CDF for a fresh feed")
    print("=" * 60)
    spark.sql(f"DROP TABLE IF EXISTS {CDF_HISTORY_TABLE}")
    spark.sql(f"DROP TABLE IF EXISTS {CDF_DECISIONS_HISTORY}")
    print(f"\u2705 Dropped stale history tables: {CDF_HISTORY_TABLE}, {CDF_DECISIONS_HISTORY}")
    spark.sql(f"""
        MERGE INTO {CDF_WATERMARK_TABLE} w
        USING (SELECT col1 AS table_name, CAST(-1 AS BIGINT) AS last_lsn,
                      current_timestamp() AS updated_at
               FROM VALUES ('{UC_ACTION_CATALOG}'), ('{UC_DECISIONS}')) s
        ON w.table_name = s.table_name
        WHEN MATCHED THEN UPDATE SET last_lsn = s.last_lsn, updated_at = s.updated_at
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"\u2705 Watermarks reset to -1 for {UC_ACTION_CATALOG} + {UC_DECISIONS}")
else:
    # Normal run \u2014 ensure watermark rows exist (start at -1 on first ever run).
    spark.sql(f"""
        MERGE INTO {CDF_WATERMARK_TABLE} w
        USING (SELECT col1 AS table_name, CAST(-1 AS BIGINT) AS last_lsn,
                      current_timestamp() AS updated_at
               FROM VALUES ('{UC_ACTION_CATALOG}'), ('{UC_DECISIONS}')) s
        ON w.table_name = s.table_name
        WHEN NOT MATCHED THEN INSERT *
    """)
    print("\n\u2139\ufe0f  CDF watermark rows ensured (reconcile advances them).")

# The actual turning-on of CDF happens in the next cell (via the API).


# COMMAND ----------

# DBTITLE 1,Cell 10 - Enable Lakebase CDF on the app-writes branch (via API)
# ---------------------------------------------------------------------------
# Turns ON Change Data Feed for the app-writes branch's Postgres schema so that
# every INSERT/UPDATE/DELETE on action_catalog is materialized as a Delta table
# in Unity Catalog (<cdf_catalog>.<cdf_schema>.lb_action_catalog_history). The
# reconcile job then consumes that Delta table.
#
# This calls the Lakebase CDF REST API, so it is FULLY AUTOMATED - no UI click.
# If the API is unavailable in a given workspace (older release), the cell
# degrades gracefully and prints the equivalent one-time manual UI step instead,
# so the notebook always completes successfully.
#
# It only (re-)enables when the branch is fresh (_NEEDS_CDF_ENABLE). On a normal
# steady-state run it just checks and reports status.
#
# Idempotency: creating a config for a schema that already has one is treated as
# success. A reset run deletes the existing config first (force=true also removes
# the old Delta output) and recreates it cleanly.
#
# API shape (all under the Lakebase "postgres" surface):
#   parent = projects/{project}/branches/{branch}/databases/{database_id}
#   LIST   GET    /api/2.0/postgres/{parent}/cdf-configs
#   CREATE POST   /api/2.0/postgres/{parent}/cdf-configs?cdf_config_id={id}
#                 body: {catalog, schema, postgres_schema}   (flat, not nested)
#   STATUS GET    /api/2.0/postgres/{parent}/cdf-configs/{id}/cdf-statuses
#   DELETE DELETE /api/2.0/postgres/{parent}/cdf-configs/{id}?force=true
# The destination UC schema (CDF_CATALOG.CDF_SCHEMA) must already exist - Cell 9
# created it. The CDF config id is the Postgres schema name (matches the UI).
# ---------------------------------------------------------------------------
import time
from databricks.sdk import WorkspaceClient

# Resource path pieces for the CDF API. LAKEBASE_DATABASE_ID is the hyphenated
# database RESOURCE id (e.g. databricks-postgres), not the SQL dbname.
_CDF_PARENT = (f"projects/{LAKEBASE_PROJECT}/branches/{APP_WRITES_BRANCH_ID}"
               f"/databases/{LAKEBASE_DATABASE_ID}")
_CDF_CONFIG_ID = LAKEBASE_SCHEMA                       # matches the UI default
_CDF_CONFIGS_PATH = f"/api/2.0/postgres/{_CDF_PARENT}/cdf-configs"
_CDF_CONFIG_PATH = f"{_CDF_CONFIGS_PATH}/{_CDF_CONFIG_ID}"

# Use the SDK's authenticated low-level REST client so we inherit workspace auth
# and never hardcode a host/token.
_w_cdf = WorkspaceClient()
_api = _w_cdf.api_client


def _cdf_list_config_ids():
    """Return the set of existing CDF config ids on the app-writes branch."""
    resp = _api.do("GET", _CDF_CONFIGS_PATH)
    return {c.get("cdf_config_id") for c in (resp or {}).get("cdf_configs", [])}


def _cdf_create_config():
    """Create (enable) the CDF config. Flat body; id passed as a query param."""
    return _api.do(
        "POST", _CDF_CONFIGS_PATH,
        query={"cdf_config_id": _CDF_CONFIG_ID},
        body={"catalog": CDF_CATALOG,
              "schema": CDF_SCHEMA,
              "postgres_schema": LAKEBASE_SCHEMA},
    )


def _cdf_delete_config():
    """Delete the CDF config; force=true also drops the replicated Delta table."""
    return _api.do("DELETE", _CDF_CONFIG_PATH, query={"force": "true"})


def _cdf_wait_streaming(timeout_s=120, poll_s=6):
    """Poll per-table CDF statuses until action_catalog is STREAMING (or timeout).
    Returns the matching status dict, or None if it never reached streaming."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = _api.do("GET", f"{_CDF_CONFIG_PATH}/cdf-statuses")
        for st in (resp or {}).get("cdf_statuses", []):
            if st.get("postgres_table") == "action_catalog":
                if st.get("state") == "CDF_STATE_STREAMING":
                    return st
        time.sleep(poll_s)
    return None


def _print_manual_cdf_banner(reason):
    """Fallback: print the one-time manual UI step if the API path is unavailable."""
    print("\n" + "=" * 60)
    print("ENABLE CDF MANUALLY (API unavailable - fallback)")
    print("=" * 60)
    print(reason)
    print("Do this once in the Databricks UI (CDF does not survive a re-fork):")
    print(f"  1. Compute -> Lakebase -> project '{LAKEBASE_PROJECT}'")
    print(f"     -> branch '{APP_WRITES_BRANCH_ID}' -> 'Lakebase CDF' tab -> Start")
    print(f"  2. source:      {LAKEBASE_SCHEMA}.action_catalog")
    print(f"     destination: catalog '{CDF_CATALOG}', schema '{CDF_SCHEMA}'")
    print(f"                  (table auto-named lb_action_catalog_history)")
    print("  3. Confirm status shows 'Enabled' with a 'Committed LSN'.")
    print("=" * 60)


if not _NEEDS_CDF_ENABLE:
    # Steady-state run: the branch already existed, so CDF is already configured.
    # Just report status so the run log confirms the feed is healthy.
    print("app-writes branch already existed - CDF should already be enabled.")
    try:
        _existing = _cdf_list_config_ids()
        if _CDF_CONFIG_ID in _existing:
            _st = _cdf_wait_streaming(timeout_s=1, poll_s=1)  # single quick check
            if _st:
                print(f"CDF config '{_CDF_CONFIG_ID}' present; action_catalog is "
                      f"STREAMING (committed_lsn {_st.get('committed_lsn')}).")
            else:
                print(f"CDF config '{_CDF_CONFIG_ID}' present; status pending.")
        else:
            _print_manual_cdf_banner("No CDF config found on an existing branch.")
    except Exception as _e:
        print(f"Could not query CDF status (non-fatal): {_e}")
else:
    # Fresh branch (first-ever create OR reset): (re-)enable CDF via the API.
    print("\n" + "=" * 60)
    print("ENABLING CDF ON app-writes (via Lakebase CDF API)")
    print("=" * 60)
    print(f"  parent:          {_CDF_PARENT}")
    print(f"  cdf_config_id:   {_CDF_CONFIG_ID}")
    print(f"  postgres schema: {LAKEBASE_SCHEMA}")
    print(f"  UC destination:  {CDF_CATALOG}.{CDF_SCHEMA} "
          f"(table -> {CDF_HISTORY_TABLE})")
    try:
        _existing = _cdf_list_config_ids()

        # On a reset the branch was re-forked; delete any pre-existing config so
        # the feed starts cleanly (force=true also clears the old Delta output).
        if _RESET and _CDF_CONFIG_ID in _existing:
            print("  reset: deleting the existing CDF config first ...")
            _cdf_delete_config()
            time.sleep(3)
            _existing = _cdf_list_config_ids()

        if _CDF_CONFIG_ID in _existing:
            print(f"CDF config '{_CDF_CONFIG_ID}' already exists - leaving it in place.")
        else:
            _cdf_create_config()
            print(f"Created CDF config '{_CDF_CONFIG_ID}'.")

        # Confirm the feed actually starts streaming before declaring success.
        _st = _cdf_wait_streaming(timeout_s=120, poll_s=6)
        if _st:
            print(f"CDF STREAMING for action_catalog -> {_st.get('uc_table')}")
            print(f"   committed_lsn={_st.get('committed_lsn')} "
                  f"last_sync={_st.get('last_sync_time')}")
            print("\nCDF enabled automatically - no manual step. Run nba_reconcile "
                  "after an app edit to publish it.")
        else:
            print("CDF config created but did not report STREAMING within the")
            print("timeout. It usually starts within ~30s - re-check with:")
            print(f"    SELECT _pg_change_type, count(*) FROM {CDF_HISTORY_TABLE} GROUP BY 1;")
    except Exception as _e:
        # Any failure (API not available, permissions, older workspace) is
        # non-fatal: the notebook still completes, and we hand off the one-time
        # manual UI step so the operator can enable CDF by hand.
        _print_manual_cdf_banner(f"CDF API call failed ({type(_e).__name__}: {_e}).")
