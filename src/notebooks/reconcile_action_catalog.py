# Databricks notebook source
# DBTITLE 1,Architecture Diagram
# MAGIC %md
# MAGIC ## 🖼️ Reconciliation Flow (CDF-based)
# MAGIC
# MAGIC ![Reconcile flow](../../images/image_1786763111330.png "Reconcile flow")

# COMMAND ----------

# DBTITLE 1,Notebook Overview
# MAGIC %md
# MAGIC ## 📚 Notebook Overview: `reconcile_action_catalog` (CDF)
# MAGIC
# MAGIC **Purpose:** Publish business-user edits to `action_catalog` from the Lakebase
# MAGIC **app-writes** branch into the governed Unity Catalog Delta table, then sync
# MAGIC to the **production** branch — using **Lakebase Change Data Feed (CDF)**
# MAGIC instead of a Postgres trigger + change-log table.
# MAGIC
# MAGIC **How it works (all Spark SQL — no psycopg2 for change capture):**
# MAGIC 1. Read the last processed LSN from a watermark table.
# MAGIC 2. Read the CDF history table for rows with `_pg_lsn > last_lsn`.
# MAGIC 3. Collapse to the newest change per `action_id` (ignore `update_preimage`).
# MAGIC 4. MERGE net changes (upserts + deletes) into the UC Delta table.
# MAGIC 5. Sync the full UC table to the production Lakebase branch.
# MAGIC 6. Advance the watermark to `max(_pg_lsn)`.
# MAGIC
# MAGIC **Key differences from the trigger-based version (kept as
# MAGIC `reconcile_action_catalog_backup.py`):**
# MAGIC - No `_action_catalog_changes` table, no trigger, no stored-proc read logic.
# MAGIC - **No branch re-fork** — the app-writes branch is permanent; the LSN
# MAGIC   watermark replaces log-clearing.
# MAGIC - Change capture is a managed Delta feed in Unity Catalog.
# MAGIC
# MAGIC **Prerequisite:** CDF must be enabled on the app-writes branch's
# MAGIC `action_catalog`. This is done automatically by `nba_bootstrap` (via the
# MAGIC Lakebase CDF API); if it is somehow not enabled, Step 1 below detects it,
# MAGIC reports the CDF status, and exits cleanly with `CDF_NOT_ENABLED`.

# COMMAND ----------

# DBTITLE 1,Install SDK (for production sync)
# MAGIC %pip install --upgrade "databricks-sdk>=0.118.0" psycopg2-binary --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration
import json
import pandas as pd

# All config from widgets/job params (bundle passes these per target).
dbutils.widgets.text("uc_catalog", "nba_demo", "UC catalog")
dbutils.widgets.text("uc_schema", "nba_new", "UC schema")
dbutils.widgets.text("lakebase_database", "databricks_postgres", "Lakebase database (SQL dbname)")
dbutils.widgets.text("lakebase_database_id", "databricks-postgres", "Lakebase database RESOURCE id (hyphenated)")
dbutils.widgets.text("lakebase_schema", "nba_new_lbase", "Lakebase (Postgres) schema")
dbutils.widgets.text("lakebase_project", "lakebase-demo-autoscale", "Lakebase project id")
dbutils.widgets.text("lakebase_branch", "production", "Lakebase production branch")
dbutils.widgets.text("app_writes_branch", "app-writes", "Lakebase app-writes branch")
dbutils.widgets.text("cdf_catalog", "nba_demo", "CDF catalog (history + watermark)")
dbutils.widgets.text("cdf_schema", "cdf", "CDF schema (history + watermark)")

UC_CATALOG = dbutils.widgets.get("uc_catalog")
UC_SCHEMA = dbutils.widgets.get("uc_schema")
DATABASE = dbutils.widgets.get("lakebase_database")
DATABASE_ID = dbutils.widgets.get("lakebase_database_id")   # hyphenated resource id
SCHEMA = dbutils.widgets.get("lakebase_schema")
PROJECT = dbutils.widgets.get("lakebase_project")
PRODUCTION_BRANCH_ID = dbutils.widgets.get("lakebase_branch")
APP_WRITES_BRANCH_ID = dbutils.widgets.get("app_writes_branch")
CDF_CATALOG = dbutils.widgets.get("cdf_catalog")
CDF_SCHEMA = dbutils.widgets.get("cdf_schema")

UC_TABLE = f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog"
# Lakebase CDF names the destination table lb_<pg_table>_history (the UI's fixed
# convention when you "Start" the feed on a schema). Keep this in sync with what
# the Lakebase CDF UI creates for nba_new_lbase.action_catalog.
CDF_HISTORY_TABLE = f"{CDF_CATALOG}.{CDF_SCHEMA}.lb_action_catalog_history"
CDF_WATERMARK_TABLE = f"{CDF_CATALOG}.{CDF_SCHEMA}.action_catalog_watermark"
PRODUCTION_ENDPOINT = f"projects/{PROJECT}/branches/{PRODUCTION_BRANCH_ID}/endpoints/primary"

# Business columns synced between Lakebase, UC, and production.
ACTION_COLUMNS = [
    "action_id", "action_name", "action_category", "team_owner", "description",
    "value_score", "compliance_flag", "strategic_priority", "eligible_channels",
    "min_spacing_days",
]

print("Configuration loaded.")
print(f"  UC table:   {UC_TABLE}")
print(f"  CDF history:{CDF_HISTORY_TABLE}")
print(f"  Watermark:  {CDF_WATERMARK_TABLE}")

# COMMAND ----------

# DBTITLE 1,Step 1 — Read watermark + guard for CDF not enabled
# ---------------------------------------------------------------------------
# Reads the last-processed LSN from the watermark table, and guards against the
# case where Lakebase CDF has not been enabled on the app-writes branch yet
# (in which case the destination Delta history table won't exist). When CDF is
# missing we query the CDF status API to report precisely why, and point at the
# fix (re-run nba_bootstrap, which enables CDF automatically via the API).
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F
from pyspark.sql.utils import AnalysisException

# Ensure watermark table exists (sync notebook creates it; be self-sufficient).
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CDF_CATALOG}.{CDF_SCHEMA}")
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {CDF_WATERMARK_TABLE} (
        table_name STRING, last_lsn BIGINT, updated_at TIMESTAMP
    )
""")


def _cdf_status_summary():
    """Best-effort: query the Lakebase CDF status API for action_catalog.
    Returns a short human string, or None if the API/config is unavailable."""
    try:
        from databricks.sdk import WorkspaceClient
        api = WorkspaceClient().api_client
        parent = (f"projects/{PROJECT}/branches/{APP_WRITES_BRANCH_ID}"
                  f"/databases/{DATABASE_ID}/cdf-configs/{SCHEMA}")
        resp = api.do("GET", f"/api/2.0/postgres/{parent}/cdf-statuses")
        for st in (resp or {}).get("cdf_statuses", []):
            if st.get("postgres_table") == "action_catalog":
                return f"state={st.get('state')} committed_lsn={st.get('committed_lsn')}"
        return "no action_catalog status found (config exists but table not tracked)"
    except Exception as e:
        return f"CDF status API unavailable ({type(e).__name__})"


# Guard: is the CDF history table present? If not, CDF likely isn't enabled yet.
try:
    hist_count = spark.table(CDF_HISTORY_TABLE).count()
except AnalysisException:
    print("=" * 60)
    print("CDF history table not found:")
    print(f"    {CDF_HISTORY_TABLE}")
    print("=" * 60)
    print("Lakebase CDF is not enabled on the app-writes branch yet.")
    print(f"CDF status: {_cdf_status_summary()}")
    print("\nFIX: re-run the bootstrap job — it enables CDF automatically via the")
    print("Lakebase CDF API:")
    print("    databricks bundle run nba_bootstrap -t <target>")
    print("(If the CDF API is unavailable in this workspace, the bootstrap prints")
    print(" the one-time manual UI step instead.)")
    print("See design/CDF_reconciliation_design.md.")
    dbutils.notebook.exit("CDF_NOT_ENABLED")

# Read last processed LSN (missing row => -1 => process from start).
row = (spark.table(CDF_WATERMARK_TABLE)
       .where(F.col("table_name") == UC_TABLE)
       .agg(F.max("last_lsn").alias("m")).collect()[0])
last_lsn = row["m"] if row["m"] is not None else -1
print(f"History rows total: {hist_count}")
print(f"Last processed LSN: {last_lsn}")

# Belt-and-suspenders: if the stored watermark is higher than anything in the
# (possibly freshly re-created) history table, a reset likely happened without
# clearing it. Auto-reset to -1 so we don't silently skip the new feed.
max_lsn_all = spark.table(CDF_HISTORY_TABLE).agg(F.max("_pg_lsn").alias("m")).collect()[0]["m"]
if max_lsn_all is not None and last_lsn > max_lsn_all:
    print(f"⚠️  Watermark ({last_lsn}) > max history LSN ({max_lsn_all}); "
          f"feed was likely reset. Resetting watermark to -1.")
    last_lsn = -1

# COMMAND ----------

# DBTITLE 1,Step 2 — Collapse CDF to newest change per action_id
# Newest change per key in the new slice; ignore update_preimage (never final).
spark.sql(f"""
    CREATE OR REPLACE TEMP VIEW cdf_net AS
    WITH slice AS (
        SELECT * FROM {CDF_HISTORY_TABLE}
        WHERE _pg_lsn > {last_lsn}
          AND _pg_change_type <> 'update_preimage'
    ),
    ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY action_id
                                  ORDER BY _pg_lsn DESC, _sort_by DESC) AS rn
        FROM slice
    )
    SELECT *, (_pg_change_type = 'delete') AS _is_delete
    FROM ranked WHERE rn = 1
""")

net = spark.table("cdf_net")
n_changes = net.count()
new_max_lsn = net.agg(F.max("_pg_lsn").alias("m")).collect()[0]["m"]
print(f"Net changes in this slice: {n_changes}")

if n_changes == 0:
    print("\n✅ No new changes since last watermark. Nothing to reconcile.")
    dbutils.notebook.exit("NO_CHANGES")

n_del = net.where(F.col("_is_delete")).count()
print(f"  upserts: {n_changes - n_del} | deletes: {n_del}")
display(net.select("action_id", "_pg_change_type", "_pg_lsn", "_sort_by"))

# COMMAND ----------

# DBTITLE 1,Step 3 — MERGE net changes into UC (upserts + deletes)
# Normalize eligible_channels to a Spark array<string> for the MERGE.
# In the CDF feed a JSONB/array column typically arrives as a JSON string.
def _to_array(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    try:
        v = json.loads(x)
        return v if isinstance(v, list) else [str(v)]
    except Exception:
        # Postgres array text like {"Digital","Call center"}
        s = str(x).strip().strip("{}")
        return [p.strip().strip('"') for p in s.split(",")] if s else []

net_pd = net.select(*ACTION_COLUMNS, "_is_delete").toPandas()
if "eligible_channels" in net_pd.columns:
    net_pd["eligible_channels"] = net_pd["eligible_channels"].apply(_to_array)

staging = spark.createDataFrame(net_pd)
staging.createOrReplaceTempView("reconcile_staging")

spark.sql(f"""
    MERGE INTO {UC_TABLE} t
    USING reconcile_staging s
    ON t.action_id = s.action_id
    WHEN MATCHED AND s._is_delete THEN DELETE
    WHEN MATCHED THEN UPDATE SET
        action_name = s.action_name,
        action_category = s.action_category,
        team_owner = s.team_owner,
        description = s.description,
        value_score = s.value_score,
        compliance_flag = s.compliance_flag,
        strategic_priority = s.strategic_priority,
        eligible_channels = s.eligible_channels,
        min_spacing_days = s.min_spacing_days
    WHEN NOT MATCHED AND NOT s._is_delete THEN INSERT (
        action_id, action_name, action_category, team_owner, description,
        value_score, compliance_flag, strategic_priority, eligible_channels, min_spacing_days
    ) VALUES (
        s.action_id, s.action_name, s.action_category, s.team_owner, s.description,
        s.value_score, s.compliance_flag, s.strategic_priority, s.eligible_channels, s.min_spacing_days
    )
""")
print(f"✅ MERGE complete into {UC_TABLE} "
      f"({n_changes - n_del} upserts, {n_del} deletes)")

# COMMAND ----------

# DBTITLE 1,Step 4 — Sync UC to Lakebase production branch
import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
db_user = w.current_user.me().user_name

ep_prod = w.postgres.get_endpoint(name=PRODUCTION_ENDPOINT)
PRODUCTION_HOST = ep_prod.status.hosts.host
token_prod = w.postgres.generate_database_credential(endpoint=PRODUCTION_ENDPOINT).token
print(f"✅ Resolved production host: {PRODUCTION_HOST}")

conn_prod = psycopg2.connect(
    host=PRODUCTION_HOST, port=5432, dbname=DATABASE,
    user=db_user, password=token_prod, sslmode="require",
)
conn_prod.autocommit = True

uc_actions = spark.table(UC_TABLE).toPandas()
with conn_prod.cursor() as cur:
    cur.execute(f"TRUNCATE TABLE {SCHEMA}.action_catalog")
    for _, r in uc_actions.iterrows():
        # eligible_channels is JSONB in Lakebase. Spark array<string> arrives from
        # .toPandas() as a numpy ndarray (NOT a Python list), so normalize to a
        # list first, then write JSON — matches how sync_nba_to_lakebase writes it.
        channels = r.get("eligible_channels", None)
        if channels is None:
            channels_list = []
        elif isinstance(channels, str):
            try:
                channels_list = json.loads(channels)
            except Exception:
                channels_list = [channels]
        else:
            channels_list = list(channels)  # handles numpy ndarray + list
        channels_pg = json.dumps(channels_list)
        cur.execute(f"""
            INSERT INTO {SCHEMA}.action_catalog
            (action_id, action_name, action_category, team_owner, description,
             value_score, compliance_flag, strategic_priority, eligible_channels,
             min_spacing_days)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            r["action_id"], r["action_name"], r["action_category"],
            r["team_owner"], r.get("description", ""),
            float(r["value_score"]), bool(r["compliance_flag"]),
            int(r["strategic_priority"]), channels_pg, int(r["min_spacing_days"]),
        ))
print(f"✅ Synced {len(uc_actions)} actions to production branch")
conn_prod.close()

# COMMAND ----------

# DBTITLE 1,Step 5 — Advance the watermark
# Only after the MERGE + production sync succeeded. Idempotent + restart-safe.
spark.sql(f"""
    MERGE INTO {CDF_WATERMARK_TABLE} w
    USING (SELECT '{UC_TABLE}' AS table_name,
                  CAST({new_max_lsn} AS BIGINT) AS last_lsn,
                  current_timestamp() AS updated_at) s
    ON w.table_name = s.table_name
    WHEN MATCHED THEN UPDATE SET last_lsn = s.last_lsn, updated_at = s.updated_at
    WHEN NOT MATCHED THEN INSERT *
""")
print(f"✅ Watermark advanced to {new_max_lsn} for {UC_TABLE}")
print("\n🎉 RECONCILIATION COMPLETE (CDF)")
print(f"   Changes applied: {n_changes} ({n_del} deletes)")
print(f"   UC updated + production synced. No branch re-fork (permanent branch).")
