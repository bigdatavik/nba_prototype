# Databricks notebook source
# DBTITLE 1,Notebook Overview
# MAGIC %md
# MAGIC ## 📚 Notebook Overview: `reconcile_nba_decisions` (CDF)
# MAGIC
# MAGIC **Purpose:** Publish the app's **Approve & Act** decisions from the Lakebase
# MAGIC **app-writes** branch (`nba_decisions`) into a governed Unity Catalog Delta
# MAGIC table — the *learning half* of the closed loop — using **Lakebase Change Data
# MAGIC Feed (CDF)**. This makes decisions + outcomes queryable by Genie / AI-BI and
# MAGIC available for retraining on real labels.
# MAGIC
# MAGIC **How it works (all Spark SQL):**
# MAGIC 1. Read the last processed LSN from the shared watermark table.
# MAGIC 2. Read `lb_nba_decisions_history` for rows with `_pg_lsn > last_lsn`.
# MAGIC 3. Collapse to the newest change per `decision_id` (ignore `update_preimage`).
# MAGIC 4. MERGE net changes (upserts + deletes) into the governed UC table.
# MAGIC 5. Advance the watermark to `max(_pg_lsn)`.
# MAGIC
# MAGIC **No production re-sync** — unlike `action_catalog`, decisions are operational
# MAGIC app state; the app reads them from app-writes, not from the production branch.
# MAGIC
# MAGIC **Prerequisite:** CDF enabled on the app-writes branch's `nba_decisions`
# MAGIC (REPLICA IDENTITY FULL). `nba_bootstrap` sets this up automatically. If it is
# MAGIC not enabled, Step 1 reports the status and exits cleanly with `CDF_NOT_ENABLED`.

# COMMAND ----------

# DBTITLE 1,Configuration
dbutils.widgets.text("uc_catalog", "nba_demo", "UC catalog")
dbutils.widgets.text("uc_schema", "nba_new", "UC schema")
dbutils.widgets.text("lakebase_database_id", "databricks-postgres", "Lakebase database RESOURCE id (hyphenated)")
dbutils.widgets.text("lakebase_schema", "nba_new_lbase", "Lakebase (Postgres) schema")
dbutils.widgets.text("lakebase_project", "lakebase-demo-autoscale", "Lakebase project id")
dbutils.widgets.text("app_writes_branch", "app-writes", "Lakebase app-writes branch")
dbutils.widgets.text("cdf_catalog", "nba_demo", "CDF catalog (history + watermark)")
dbutils.widgets.text("cdf_schema", "cdf", "CDF schema (history + watermark)")

UC_CATALOG = dbutils.widgets.get("uc_catalog")
UC_SCHEMA = dbutils.widgets.get("uc_schema")
DATABASE_ID = dbutils.widgets.get("lakebase_database_id")
SCHEMA = dbutils.widgets.get("lakebase_schema")
PROJECT = dbutils.widgets.get("lakebase_project")
APP_WRITES_BRANCH_ID = dbutils.widgets.get("app_writes_branch")
CDF_CATALOG = dbutils.widgets.get("cdf_catalog")
CDF_SCHEMA = dbutils.widgets.get("cdf_schema")

UC_TABLE = f"{UC_CATALOG}.{UC_SCHEMA}.nba_decisions"
CDF_HISTORY_TABLE = f"{CDF_CATALOG}.{CDF_SCHEMA}.lb_nba_decisions_history"
CDF_WATERMARK_TABLE = f"{CDF_CATALOG}.{CDF_SCHEMA}.action_catalog_watermark"  # shared, keyed by table_name

# Business columns of the decision log.
DECISION_COLUMNS = [
    "decision_id", "member_id", "action_id", "action_name", "channel",
    "recommended_score", "status", "disposition", "outcome", "note",
    "approver", "created_at",
]

print("Configuration loaded.")
print(f"  UC table:    {UC_TABLE}")
print(f"  CDF history: {CDF_HISTORY_TABLE}")
print(f"  Watermark:   {CDF_WATERMARK_TABLE}")

# COMMAND ----------

# DBTITLE 1,Step 1 — Ensure governed table + watermark; guard for CDF not enabled
from pyspark.sql import functions as F
from pyspark.sql.utils import AnalysisException

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {UC_CATALOG}.{UC_SCHEMA}")
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {UC_TABLE} (
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
    COMMENT 'Governed net-state NBA decision log (reconciled from Lakebase CDF lb_nba_decisions_history). The learning half of the Approve & Act closed loop.'
""")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CDF_CATALOG}.{CDF_SCHEMA}")
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {CDF_WATERMARK_TABLE} (
        table_name STRING, last_lsn BIGINT, updated_at TIMESTAMP
    )
""")


def _cdf_status_summary():
    try:
        from databricks.sdk import WorkspaceClient
        api = WorkspaceClient().api_client
        parent = (f"projects/{PROJECT}/branches/{APP_WRITES_BRANCH_ID}"
                  f"/databases/{DATABASE_ID}/cdf-configs/{SCHEMA}")
        resp = api.do("GET", f"/api/2.0/postgres/{parent}/cdf-statuses")
        for st in (resp or {}).get("cdf_statuses", []):
            if st.get("postgres_table") == "nba_decisions":
                return f"state={st.get('state')} committed_lsn={st.get('committed_lsn')}"
        return "no nba_decisions status found (config exists but table not tracked)"
    except Exception as e:
        return f"CDF status API unavailable ({type(e).__name__})"


try:
    hist_count = spark.table(CDF_HISTORY_TABLE).count()
except AnalysisException:
    print("=" * 60)
    print(f"CDF history table not found: {CDF_HISTORY_TABLE}")
    print("=" * 60)
    print("Lakebase CDF is not capturing nba_decisions yet.")
    print(f"CDF status: {_cdf_status_summary()}")
    print("\nFIX: re-run nba_bootstrap — it pre-creates nba_decisions with")
    print("REPLICA IDENTITY FULL and enables CDF automatically via the API:")
    print("    databricks bundle run nba_bootstrap -t <target>")
    dbutils.notebook.exit("CDF_NOT_ENABLED")

row = (spark.table(CDF_WATERMARK_TABLE)
       .where(F.col("table_name") == UC_TABLE)
       .agg(F.max("last_lsn").alias("m")).collect()[0])
last_lsn = row["m"] if row["m"] is not None else -1
print(f"History rows total: {hist_count}")
print(f"Last processed LSN: {last_lsn}")

max_lsn_all = spark.table(CDF_HISTORY_TABLE).agg(F.max("_pg_lsn").alias("m")).collect()[0]["m"]
if max_lsn_all is not None and last_lsn > max_lsn_all:
    print(f"⚠️  Watermark ({last_lsn}) > max history LSN ({max_lsn_all}); "
          f"feed likely reset. Resetting watermark to -1.")
    last_lsn = -1

# COMMAND ----------

# DBTITLE 1,Step 2 — Collapse CDF to newest change per decision_id
spark.sql(f"""
    CREATE OR REPLACE TEMP VIEW dec_cdf_net AS
    WITH slice AS (
        SELECT * FROM {CDF_HISTORY_TABLE}
        WHERE _pg_lsn > {last_lsn}
          AND _pg_change_type <> 'update_preimage'
    ),
    ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY decision_id
                                  ORDER BY _pg_lsn DESC, _sort_by DESC) AS rn
        FROM slice
    )
    SELECT *, (_pg_change_type = 'delete') AS _is_delete
    FROM ranked WHERE rn = 1
""")

net = spark.table("dec_cdf_net")
n_changes = net.count()
if n_changes == 0:
    print("\n✅ No new decisions since last watermark. Nothing to reconcile.")
    dbutils.notebook.exit("NO_CHANGES")

new_max_lsn = net.agg(F.max("_pg_lsn").alias("m")).collect()[0]["m"]
n_del = net.where(F.col("_is_delete")).count()
print(f"Net changes: {n_changes}  (upserts: {n_changes - n_del} | deletes: {n_del})")

# COMMAND ----------

# DBTITLE 1,Step 3 — MERGE net changes into the governed UC table
staging = net.select(*DECISION_COLUMNS, "_is_delete")
staging.createOrReplaceTempView("dec_reconcile_staging")

spark.sql(f"""
    MERGE INTO {UC_TABLE} t
    USING dec_reconcile_staging s
    ON t.decision_id = s.decision_id
    WHEN MATCHED AND s._is_delete THEN DELETE
    WHEN MATCHED THEN UPDATE SET
        member_id = s.member_id,
        action_id = s.action_id,
        action_name = s.action_name,
        channel = s.channel,
        recommended_score = s.recommended_score,
        status = s.status,
        disposition = s.disposition,
        outcome = s.outcome,
        note = s.note,
        approver = s.approver,
        created_at = s.created_at
    WHEN NOT MATCHED AND NOT s._is_delete THEN INSERT (
        decision_id, member_id, action_id, action_name, channel, recommended_score,
        status, disposition, outcome, note, approver, created_at
    ) VALUES (
        s.decision_id, s.member_id, s.action_id, s.action_name, s.channel, s.recommended_score,
        s.status, s.disposition, s.outcome, s.note, s.approver, s.created_at
    )
""")
print(f"✅ MERGE complete into {UC_TABLE} "
      f"({n_changes - n_del} upserts, {n_del} deletes)")

# COMMAND ----------

# DBTITLE 1,Step 4 — Advance the watermark
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
print("\n🎉 DECISIONS RECONCILIATION COMPLETE (CDF)")
print(f"   Governed UC table updated: {UC_TABLE}")
