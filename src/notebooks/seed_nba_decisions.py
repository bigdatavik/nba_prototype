# Databricks notebook source
# DBTITLE 1,Notebook Overview
# MAGIC %md
# MAGIC # 🌱 Seed dummy NBA decisions (Approve & Act)
# MAGIC
# MAGIC Generates realistic **synthetic decisions** into the Lakebase app-writes
# MAGIC `nba_decisions` table so the Approve & Act log, the governed UC table, and
# MAGIC the Genie room have enough volume to be interesting in a demo.
# MAGIC
# MAGIC Flow: this notebook INSERTs into Lakebase → **Lakebase CDF** captures it →
# MAGIC run `nba_reconcile_decisions` to publish net-state to the governed UC table
# MAGIC `<uc_catalog>.<uc_schema>.nba_decisions`.
# MAGIC
# MAGIC Idempotent-ish: it appends `num_decisions` rows each run (clear the table
# MAGIC first if you want an exact count). Members + actions are read from the UC
# MAGIC source tables so ids always match the rest of the prototype.

# COMMAND ----------

# DBTITLE 1,Install SDK
# MAGIC %pip install --upgrade "databricks-sdk>=0.118.0" psycopg2-binary --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration
dbutils.widgets.text("uc_catalog", "nba_demo", "UC catalog")
dbutils.widgets.text("uc_schema", "nba_new", "UC schema")
dbutils.widgets.text("lakebase_project", "lakebase-demo-autoscale", "Lakebase project id")
dbutils.widgets.text("app_writes_branch", "app-writes", "Lakebase app-writes branch")
dbutils.widgets.text("lakebase_database", "databricks_postgres", "Lakebase database (SQL dbname)")
dbutils.widgets.text("lakebase_schema", "nba_new_lbase", "Lakebase (Postgres) schema")
dbutils.widgets.text("num_decisions", "200", "Number of synthetic decisions to insert")
dbutils.widgets.dropdown("truncate_first", "false", ["true", "false"], "Truncate nba_decisions first")

UC_CATALOG = dbutils.widgets.get("uc_catalog")
UC_SCHEMA = dbutils.widgets.get("uc_schema")
PROJECT = dbutils.widgets.get("lakebase_project")
APP_WRITES_BRANCH_ID = dbutils.widgets.get("app_writes_branch")
DATABASE = dbutils.widgets.get("lakebase_database")
SCHEMA = dbutils.widgets.get("lakebase_schema")
NUM_DECISIONS = int(dbutils.widgets.get("num_decisions"))
TRUNCATE_FIRST = dbutils.widgets.get("truncate_first") == "true"

MEMBER_FEATURES = f"{UC_CATALOG}.{UC_SCHEMA}.serving_member_features"
ACTION_CATALOG = f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog"
print(f"Inserting {NUM_DECISIONS} decisions into {SCHEMA}.nba_decisions "
      f"(truncate_first={TRUNCATE_FIRST})")

# COMMAND ----------

# DBTITLE 1,Read members + actions from UC (ids always match the prototype)
member_ids = [r["member_id"] for r in
              spark.table(MEMBER_FEATURES).select("member_id").collect()]
actions = spark.table(ACTION_CATALOG).select(
    "action_id", "action_name", "action_category", "value_score",
    "eligible_channels").collect()
print(f"Members: {len(member_ids)} | Actions: {len(actions)}")

# COMMAND ----------

# DBTITLE 1,Generate synthetic decisions
import random
from datetime import date, timedelta

random.seed(7)
TODAY = date.today()
CHANNELS = ["Digital", "Call center", "Provider", "E/Mail"]
STATUSES = ["Approved", "Dismissed"]
DISPOSITIONS = ["Outreach scheduled", "Attempted", "Declined by member", "Deferred"]
OUTCOMES = ["Gap Closed", "Enrolled", "Retained", "No Response", "None"]
APPROVERS = ["a.okafor@cedar.example", "care.coordinator@cedar.example",
             "r.bhatnagar@cedar.example", "vik.malhotra@databricks.com"]

def _channels(a):
    ch = a["eligible_channels"]
    if ch is None:
        return CHANNELS
    return list(ch) if not isinstance(ch, str) else CHANNELS

rows = []
for _ in range(NUM_DECISIONS):
    m = random.choice(member_ids)
    a = random.choice(actions)
    value = float(a["value_score"] or 70)
    score = round(min(0.99, max(0.05, value / 100.0 + random.uniform(-0.2, 0.2))), 4)
    status = random.choices(STATUSES, weights=[0.8, 0.2])[0]
    channel = random.choice(_channels(a))
    if status == "Dismissed":
        disp, outcome = "Dismissed", "None"
    else:
        disp = random.choices(DISPOSITIONS, weights=[0.55, 0.2, 0.15, 0.1])[0]
        # Only accepted/scheduled outreach tends to produce a positive outcome
        if disp in ("Outreach scheduled", "Attempted"):
            outcome = random.choices(OUTCOMES, weights=[0.35, 0.15, 0.15, 0.2, 0.15])[0]
        else:
            outcome = random.choices(["No Response", "None"], weights=[0.4, 0.6])[0]
    created = TODAY - timedelta(days=random.randint(0, 120))
    rows.append((
        m, a["action_id"], a["action_name"], channel, score, status, disp,
        None if outcome == "None" else outcome, None, random.choice(APPROVERS),
        created,
    ))
print(f"Generated {len(rows)} decisions")

# COMMAND ----------

# DBTITLE 1,Insert into Lakebase app-writes (→ CDF → reconcile)
import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
db_user = w.current_user.me().user_name
parent = f"projects/{PROJECT}/branches/{APP_WRITES_BRANCH_ID}"
endpoint = host = None
for ep in w.postgres.list_endpoints(parent=parent):
    endpoint = ep.name
    host = ep.status.hosts.host
token = w.postgres.generate_database_credential(endpoint=endpoint).token
conn = psycopg2.connect(host=host, port=5432, dbname=DATABASE,
                        user=db_user, password=token, sslmode="require")
conn.autocommit = True

with conn.cursor() as cur:
    if TRUNCATE_FIRST:
        cur.execute(f"TRUNCATE TABLE {SCHEMA}.nba_decisions RESTART IDENTITY")
        print("Truncated nba_decisions")
    cur.executemany(
        f"""INSERT INTO {SCHEMA}.nba_decisions
            (member_id, action_id, action_name, channel, recommended_score,
             status, disposition, outcome, note, approver, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        rows,
    )
    cur.execute(f"SELECT count(*) FROM {SCHEMA}.nba_decisions")
    total = cur.fetchone()[0]
conn.close()
print(f"✅ Inserted {len(rows)} decisions. Table now has {total} rows.")
print("Next: run nba_reconcile_decisions to publish net-state to the governed UC table.")
