# Databricks notebook source
# DBTITLE 1,Notebook Overview
# MAGIC %md
# MAGIC # 🌱 Seed UC Source Tables (synthetic)
# MAGIC
# MAGIC Creates the two Unity Catalog source tables the rest of the NBA prototype
# MAGIC depends on, populated with **synthetic** data so the project installs from
# MAGIC nothing in a brand-new workspace:
# MAGIC
# MAGIC - `<catalog>.<schema>.serving_member_features` — 50 members × 30 numeric features
# MAGIC - `<catalog>.<schema>.action_catalog` — 16 NBA actions
# MAGIC
# MAGIC Run this **first** (before training, sync, and the app). It is idempotent:
# MAGIC it overwrites the tables each run. All names come from widgets so nothing
# MAGIC is hardcoded.
# MAGIC
# MAGIC > Replace this with your real feature pipeline when moving beyond a demo.

# COMMAND ----------

# DBTITLE 1,Configuration
dbutils.widgets.text("uc_catalog", "nba_demo", "UC catalog")
dbutils.widgets.text("uc_schema", "nba_new", "UC schema")
dbutils.widgets.text("num_members", "50", "Number of synthetic members")

UC_CATALOG = dbutils.widgets.get("uc_catalog")
UC_SCHEMA = dbutils.widgets.get("uc_schema")
NUM_MEMBERS = int(dbutils.widgets.get("num_members"))

MEMBER_FEATURES_TABLE = f"{UC_CATALOG}.{UC_SCHEMA}.serving_member_features"
ACTION_CATALOG_TABLE = f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog"

# Only attempt CREATE CATALOG when it truly doesn't exist. In workspaces with
# Default Storage (no metastore storage root), even `CREATE CATALOG IF NOT
# EXISTS` on an EXISTING catalog raises INVALID_STATE, so we guard on existence
# and let reuse of a pre-created catalog (UI-created / Default Storage) work.
_catalog_exists = spark.sql(f"SHOW CATALOGS LIKE '{UC_CATALOG}'").count() > 0
if not _catalog_exists:
    spark.sql(f"CREATE CATALOG IF NOT EXISTS {UC_CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {UC_CATALOG}.{UC_SCHEMA}")
print(f"Target: {MEMBER_FEATURES_TABLE}")
print(f"Target: {ACTION_CATALOG_TABLE}")

# COMMAND ----------

# DBTITLE 1,Generate synthetic member_features
import numpy as np
import pandas as pd

np.random.seed(42)

# 30 numeric feature columns consumed by the model (see training notebook).
rows = []
for i in range(NUM_MEMBERS):
    age = int(np.random.randint(45, 90))
    is_dual = int(np.random.rand() < 0.3)
    total_claims = int(np.random.poisson(12))
    total_interactions = int(np.random.poisson(8)) + 1
    digital = int(np.random.binomial(total_interactions, 0.45))
    phone = total_interactions - digital
    engagement = round(float(np.random.uniform(1, 10)), 2)
    churn_risk = round(float(np.random.beta(2, 5)), 4)
    raf = round(float(np.random.uniform(0.6, 2.6)), 3)
    clinical_risk = round(float(np.random.uniform(0.1, 0.95)), 3)
    has_gap = int(np.random.rand() < 0.5)
    rows.append({
        "member_id": f"M{1000 + i}",
        "age": age,
        "is_male": int(np.random.rand() < 0.5),
        "is_dual_eligible_flag": is_dual,
        "total_claims_12m": total_claims,
        "total_paid_amount_12m": round(float(total_claims * np.random.uniform(200, 2500)), 2),
        "avg_claim_amount": round(float(np.random.uniform(150, 1800)), 2),
        "high_cost_claims_12m": int(np.random.binomial(total_claims, 0.15)),
        "preventive_visits_12m": int(np.random.randint(0, 5)),
        "chronic_claims_12m": int(np.random.binomial(total_claims, 0.4)),
        "total_interactions_12m": total_interactions,
        "negative_interactions_12m": int(np.random.binomial(total_interactions, 0.2)),
        "complaints_12m": int(np.random.poisson(0.4)),
        "escalations_12m": int(np.random.poisson(0.2)),
        "avg_satisfaction_score": round(float(np.random.uniform(2.5, 5.0)), 2),
        "phone_interactions": phone,
        "digital_interactions": digital,
        "campaigns_received_12m": int(np.random.randint(0, 12)),
        "response_rate": round(float(np.random.uniform(0.0, 1.0)), 3),
        "digital_sessions_12m": int(np.random.poisson(6)),
        "churn_risk_score": churn_risk,
        "engagement_score": engagement,
        "escalation_likelihood": round(float(np.random.uniform(0, 1)), 4),
        "care_outreach_likelihood": round(float(np.random.uniform(0, 1)), 4),
        "plan_switch_propensity": round(float(np.random.uniform(0, 1)), 4),
        "raf_score": raf,
        "clinical_risk_score": clinical_risk,
        "predicted_cost_12m": round(float(np.random.uniform(2000, 60000)), 2),
        "has_preventive_gap": has_gap,
        "is_care_mgmt_candidate": int(clinical_risk > 0.7),
        "is_churn_risk": int(churn_risk > 0.5),
    })

members_pdf = pd.DataFrame(rows)
print(f"Generated {len(members_pdf)} members, {members_pdf.shape[1]} columns")

(spark.createDataFrame(members_pdf)
     .write.mode("overwrite").option("overwriteSchema", "true")
     .saveAsTable(MEMBER_FEATURES_TABLE))
print(f"✅ Wrote {MEMBER_FEATURES_TABLE}")

# COMMAND ----------

# DBTITLE 1,Generate synthetic action_catalog
from pyspark.sql import types as T

# 16 baseline actions. IDs match ORIGINAL_ACTION_IDS in sync_nba_to_lakebase
# (ACT001–ACT012, ACT014–ACT017). Categories/teams/channels match the app.
CHANNELS_ALL = ["Digital", "Call center", "Provider", "E/Mail"]
actions = [
    ("ACT001", "A1c Screening Outreach",            "STARS",      "Stars",               85, True,  1, 30, ["Digital", "Call center"]),
    ("ACT002", "Blood Pressure Check Reminder",     "STARS",      "Stars",               80, True,  1, 30, ["Digital", "Provider"]),
    ("ACT003", "Medication Adherence Nudge",        "Pharmacy",   "Pharmacy",            82, True,  2, 21, ["Digital", "Call center"]),
    ("ACT004", "Breast Cancer Screening Reminder",  "STARS",      "Stars",               78, True,  2, 45, ["Digital", "E/Mail"]),
    ("ACT005", "RAF Recapture Visit",               "MRA",        "MRA",                 90, False, 1, 60, ["Provider", "Call center"]),
    ("ACT006", "Chronic Condition Documentation",   "MRA",        "MRA",                 88, False, 1, 60, ["Provider"]),
    ("ACT007", "Care Management Enrollment",        "PCO",        "Clinical innovation", 92, False, 1, 30, ["Call center", "Provider"]),
    ("ACT008", "Retention Save Offer",              "PCO",        "PCO",                 70, False, 3, 30, ["Call center", "E/Mail"]),
    ("ACT009", "Plan Benefit Review Call",          "PCO",        "PCO",                 68, False, 3, 45, ["Call center"]),
    ("ACT010", "Loyalty Check-in",                  "PCO",        "PCO",                 60, False, 4, 30, ["Digital", "E/Mail"]),
    ("ACT011", "Digital Wellness Program Invite",   "STARS",      "Stars",               65, False, 3, 21, ["Digital"]),
    ("ACT012", "Home Health Assessment",            "Home health","Home health",         84, False, 2, 60, ["Provider", "Call center"]),
    ("ACT014", "Flu Vaccination Reminder",          "STARS",      "Stars",               74, True,  2, 30, ["Digital", "Provider"]),
    ("ACT015", "Diabetes Eye Exam Reminder",        "STARS",      "Stars",               76, True,  2, 45, ["Digital", "Provider"]),
    ("ACT016", "Statin Therapy Adherence",          "Pharmacy",   "Pharmacy",            79, True,  2, 21, ["Digital", "Call center"]),
    ("ACT017", "Annual Wellness Visit Scheduling",  "PCO",        "Clinical innovation", 86, False, 1, 60, ["Call center", "Provider"]),
]

action_rows = []
for (aid, name, cat, team, value, compliance, priority, spacing, channels) in actions:
    action_rows.append({
        "action_id": aid,
        "action_name": name,
        "action_category": cat,
        "team_owner": team,
        "description": f"{name} — {cat} action owned by {team}.",
        "value_score": int(value),
        "compliance_flag": bool(compliance),
        "strategic_priority": int(priority),
        "eligible_channels": channels,
        "min_spacing_days": int(spacing),
        # Extra governed columns referenced by reconcile_action_catalog:
        "suppression_rules": None,
        "valid_from": None,
        "valid_to": None,
    })

schema = T.StructType([
    T.StructField("action_id", T.StringType(), False),
    T.StructField("action_name", T.StringType(), True),
    T.StructField("action_category", T.StringType(), True),
    T.StructField("team_owner", T.StringType(), True),
    T.StructField("description", T.StringType(), True),
    T.StructField("value_score", T.IntegerType(), True),
    T.StructField("compliance_flag", T.BooleanType(), True),
    T.StructField("strategic_priority", T.IntegerType(), True),
    T.StructField("eligible_channels", T.ArrayType(T.StringType()), True),
    T.StructField("min_spacing_days", T.IntegerType(), True),
    T.StructField("suppression_rules", T.StringType(), True),
    T.StructField("valid_from", T.DateType(), True),
    T.StructField("valid_to", T.DateType(), True),
])

actions_df = spark.createDataFrame(action_rows, schema=schema)
(actions_df.write.mode("overwrite").option("overwriteSchema", "true")
     .saveAsTable(ACTION_CATALOG_TABLE))

# Enable Delta Change Data Feed on the UC table. NOTE: reconciliation does NOT
# use this — it uses Lakebase CDF on the app-writes branch (Postgres -> UC). This
# is kept only as a sensible default for the governed UC table (cheap, lets any
# downstream job read UC-side row changes if ever needed). Safe to remove.
spark.sql(f"ALTER TABLE {ACTION_CATALOG_TABLE} "
          f"SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
print(f"✅ Wrote {ACTION_CATALOG_TABLE} ({len(action_rows)} actions)")

# COMMAND ----------

# DBTITLE 1,Verify
print("member_features:")
display(spark.table(MEMBER_FEATURES_TABLE).limit(5))
print("action_catalog:")
display(spark.table(ACTION_CATALOG_TABLE))
print(f"\n✅ Seed complete. Next: train the model, then bootstrap Lakebase.")
