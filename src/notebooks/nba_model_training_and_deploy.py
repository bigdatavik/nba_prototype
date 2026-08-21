# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Notebook Overview
# MAGIC %md
# MAGIC # 🧠 NBA Model Training & Deployment
# MAGIC
# MAGIC ## Where This Fits in the Architecture
# MAGIC
# MAGIC This notebook handles **Phase 2** of the NBA prototype: training the scoring model and deploying it as a real-time endpoint.
# MAGIC
# MAGIC ```
# MAGIC ┌──────────────────────────────────────────────────────────┐
# MAGIC │  Phase 1: Feature Tables (already built)              │
# MAGIC │  • nba_demo.nba_new.serving_member_features       │
# MAGIC │  • nba_demo.nba_new.action_catalog                │
# MAGIC └────────────────────────────┬─────────────────────────────┘
# MAGIC                              │
# MAGIC                              ▼
# MAGIC ┌──────────────────────────────────────────────────────────┐
# MAGIC │  Phase 2: THIS NOTEBOOK                              │ ◄── You are here
# MAGIC │  • Cross-join members × actions (training data)        │
# MAGIC │  • Generate synthetic relevance labels                │
# MAGIC │  • Train LightGBM ranker (34 features)                │
# MAGIC │  • Log to MLflow + register in Unity Catalog          │
# MAGIC │  • Deploy to model serving endpoint (scale-to-zero)   │
# MAGIC └────────────────────────────┬─────────────────────────────┘
# MAGIC                              │
# MAGIC                              ▼
# MAGIC ┌──────────────────────────────────────────────────────────┐
# MAGIC │  Phase 3+: Operational Notebooks                      │
# MAGIC │  • sync_nba_to_lakebase (UC → Lakebase production)    │
# MAGIC │  • reconcile_action_catalog (CRUD → UC → re-fork)    │
# MAGIC └──────────────────────────────────────────────────────────┘
# MAGIC ```
# MAGIC
# MAGIC **Model:** LightGBM Regressor (pointwise ranking)
# MAGIC **Registered at:** `nba_demo.nba_new.nba_scoring_model` v1 (alias: `champion`)
# MAGIC **Endpoint:** `nba-scoring-endpoint` (classic, CPU Small, scale-to-zero)
# MAGIC **MLflow Experiment:** `/Users/<current-user>/nba_scoring_experiment` (defaults to the running user)
# MAGIC
# MAGIC **Key design choice:** The model is **action-agnostic** — it scores any member × action pair based on features. When the action catalog changes (new actions added via CRUD), the next scoring request automatically includes them. No retraining needed.
# MAGIC
# MAGIC **When to re-run:** Only needed if you want to retrain the model (e.g., with real outcomes instead of synthetic labels).

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install lightgbm --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration
# =====================================================================
# Configuration — read from notebook widgets/job params. The Databricks
# Asset Bundle passes these per-target (see resources/nba_jobs.yml), so
# NOTHING here is hardcoded to a workspace, catalog, or endpoint.
# =====================================================================
dbutils.widgets.text("uc_catalog", "nba_demo", "UC catalog")
dbutils.widgets.text("uc_schema", "nba_new", "UC schema")
dbutils.widgets.text("model_name", "nba_scoring_model", "Registered model name (UC)")
dbutils.widgets.text("model_endpoint_name", "nba-scoring-endpoint", "Serving endpoint name")
dbutils.widgets.text("mlflow_experiment_path", "", "MLflow experiment path (blank = current user)")

UC_CATALOG = dbutils.widgets.get("uc_catalog")
UC_SCHEMA = dbutils.widgets.get("uc_schema")
MEMBER_FEATURES_TABLE = f"{UC_CATALOG}.{UC_SCHEMA}.serving_member_features"
ACTION_CATALOG_TABLE = f"{UC_CATALOG}.{UC_SCHEMA}.action_catalog"
REGISTERED_MODEL_NAME = f"{UC_CATALOG}.{UC_SCHEMA}.{dbutils.widgets.get('model_name')}"
ENDPOINT_NAME = dbutils.widgets.get("model_endpoint_name")

# MLflow experiment path — defaults to the current user's home so it works for
# any developer without editing the notebook.
MLFLOW_EXPERIMENT_PATH = dbutils.widgets.get("mlflow_experiment_path").strip()
if not MLFLOW_EXPERIMENT_PATH:
    _user = spark.sql("SELECT current_user()").collect()[0][0]
    MLFLOW_EXPERIMENT_PATH = f"/Users/{_user}/nba_scoring_experiment"

print(f"Source tables: {MEMBER_FEATURES_TABLE}, {ACTION_CATALOG_TABLE}")
print(f"Model: {REGISTERED_MODEL_NAME}  Endpoint: {ENDPOINT_NAME}")
print(f"MLflow experiment: {MLFLOW_EXPERIMENT_PATH}")

# COMMAND ----------

# DBTITLE 1,Load member features and action catalog
import pandas as pd
import numpy as np
from pyspark.sql import functions as F

# Load serving member features (50 members, 30 numeric columns)
member_features_df = spark.table(MEMBER_FEATURES_TABLE).toPandas()
print(f"Member features: {member_features_df.shape[0]} members, {member_features_df.shape[1]} columns")

# Load action catalog
action_catalog_df = spark.table(ACTION_CATALOG_TABLE).toPandas()
print(f"Action catalog: {action_catalog_df.shape[0]} actions")

# Display sample
display(spark.table(MEMBER_FEATURES_TABLE).limit(5))

# COMMAND ----------

# DBTITLE 1,Create cross-product training data with synthetic labels
np.random.seed(42)

# Member feature columns (m_ prefix for model)
m_feature_cols = [
    'age', 'is_male', 'is_dual_eligible_flag', 'total_claims_12m', 'total_paid_amount_12m',
    'avg_claim_amount', 'high_cost_claims_12m', 'preventive_visits_12m', 'chronic_claims_12m',
    'total_interactions_12m', 'negative_interactions_12m', 'complaints_12m', 'escalations_12m',
    'avg_satisfaction_score', 'phone_interactions', 'digital_interactions',
    'campaigns_received_12m', 'response_rate', 'digital_sessions_12m',
    'churn_risk_score', 'engagement_score', 'escalation_likelihood',
    'care_outreach_likelihood', 'plan_switch_propensity', 'raf_score',
    'clinical_risk_score', 'predicted_cost_12m', 'has_preventive_gap',
    'is_care_mgmt_candidate', 'is_churn_risk'
]

# Action feature columns (a_ prefix for model)
a_feature_cols = ['value_score', 'strategic_priority', 'compliance_flag', 'min_spacing_days']

# Cross-product: every member x every action
training_rows = []
for _, member in member_features_df.iterrows():
    for _, action in action_catalog_df.iterrows():
        row = {}
        # Member features
        for col in m_feature_cols:
            row[f'm_{col}'] = float(member[col]) if col in member.index else 0.0
        # Action features
        row['a_value_score'] = float(action['value_score'])
        row['a_strategic_priority'] = float(action['strategic_priority'])
        row['a_compliance_flag'] = 1.0 if action.get('compliance_flag', False) else 0.0
        row['a_min_spacing_days'] = float(action['min_spacing_days'])
        row['action_id'] = action['action_id']
        row['member_id'] = member['member_id']
        training_rows.append(row)

train_df = pd.DataFrame(training_rows)
print(f"Training data: {train_df.shape[0]} rows (50 members x 12 actions = 600)")
print(f"Feature columns: {len(m_feature_cols) + len(a_feature_cols)} total")

# COMMAND ----------

# DBTITLE 1,Generate synthetic relevance labels
# Generate synthetic relevance labels using business logic
np.random.seed(42)

def generate_relevance(row):
    """Generate synthetic relevance score (0-1) based on business rules."""
    score = 0.2  # base score
    
    # Boost: care-gap members + STARS actions (A1c, blood pressure, med adherence)
    if row['m_has_preventive_gap'] == 1.0:
        if row['action_id'] in ['ACT001', 'ACT002', 'ACT003', 'ACT004']:
            score += 0.25
    
    # Boost: churn-risk members + retention/PCO actions
    if row['m_is_churn_risk'] == 1.0:
        if row['action_id'] in ['ACT008', 'ACT009', 'ACT010']:
            score += 0.20
    
    # Boost: care-management candidates + care management action
    if row['m_is_care_mgmt_candidate'] == 1.0:
        if row['action_id'] == 'ACT007':
            score += 0.30
    
    # Boost: digital-affinity members + digital-eligible actions
    digital_affinity = row['m_digital_sessions_12m'] / max(row['m_total_interactions_12m'], 1)
    if digital_affinity > 0.5:
        if row['action_id'] in ['ACT005', 'ACT006', 'ACT011']:
            score += 0.15
    
    # Boost: high RAF/clinical risk + MRA actions
    if row['m_raf_score'] > 1.5 or row['m_clinical_risk_score'] > 0.7:
        if row['action_id'] in ['ACT005', 'ACT006']:
            score += 0.20
    
    # Penalty: non-matching action-member pairs get reduced score
    if row['m_has_preventive_gap'] == 0.0 and row['action_id'] in ['ACT001', 'ACT002', 'ACT003']:
        score -= 0.10
    
    # Engagement factor (normalize to 0-1 scale, engagement_score is 0-10)
    engagement_norm = row['m_engagement_score'] / 10.0
    engagement_factor = 0.7 + 0.3 * engagement_norm
    score *= engagement_factor
    
    # Action value factor (value_score is 1-10)
    value_factor = 0.8 + 0.2 * (row['a_value_score'] / 10.0)
    score *= value_factor
    
    # Add Gaussian noise for realism
    score += np.random.normal(0, 0.06)
    
    # Clamp to [0, 1]
    return np.clip(score, 0.0, 1.0)

train_df['relevance_score'] = train_df.apply(generate_relevance, axis=1)

print(f"Label stats:")
print(f"  Mean: {train_df['relevance_score'].mean():.3f}")
print(f"  Std:  {train_df['relevance_score'].std():.3f}")
print(f"  Min:  {train_df['relevance_score'].min():.3f}")
print(f"  Max:  {train_df['relevance_score'].max():.3f}")
print(f"\nTop 10 highest-scoring member-action pairs:")
display(train_df.nlargest(10, 'relevance_score')[['member_id', 'action_id', 'relevance_score']])
print(f"\nBottom 5 lowest-scoring:")
display(train_df.nsmallest(5, 'relevance_score')[['member_id', 'action_id', 'relevance_score']])

# COMMAND ----------

# DBTITLE 1,Train LightGBM model and log to MLflow
import mlflow
import lightgbm as lgb
from mlflow.models import infer_signature

mlflow.set_registry_uri("databricks-uc")

# Prepare features and target
feature_columns = [f'm_{c}' for c in m_feature_cols] + [f'a_{c}' for c in a_feature_cols]
X_train = train_df[feature_columns].astype(float)
y_train = train_df['relevance_score'].values

print(f"Training features: {X_train.shape}")
print(f"Target: {y_train.shape}")

# Train LightGBM Regressor
model = lgb.LGBMRegressor(
    n_estimators=200,
    learning_rate=0.05,
    max_depth=6,
    num_leaves=31,
    min_child_samples=5,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=42,
    verbose=-1
)

model.fit(X_train, y_train)
y_pred = model.predict(X_train)

# Evaluate
from sklearn.metrics import root_mean_squared_error, r2_score
rmse = root_mean_squared_error(y_train, y_pred)
r2 = r2_score(y_train, y_pred)
print(f"\nTraining Metrics:")
print(f"  RMSE: {rmse:.4f}")
print(f"  R²:   {r2:.4f}")

# Log to MLflow
mlflow.set_experiment(MLFLOW_EXPERIMENT_PATH)

with mlflow.start_run(run_name="nba_lgbm_v1") as run:
    mlflow.log_params({
        "n_estimators": 200,
        "learning_rate": 0.05,
        "max_depth": 6,
        "num_members": 50,
        "num_actions": 12,
        "training_rows": len(train_df),
        "num_features": len(feature_columns)
    })
    mlflow.log_metrics({"rmse": rmse, "r2": r2})
    
    # Infer signature
    signature = infer_signature(X_train, y_pred)
    
    # Log model
    model_info = mlflow.lightgbm.log_model(
        model,
        name="nba_scoring_model",
        signature=signature,
        input_example=X_train.iloc[:3],
    )
    
    run_id = run.info.run_id
    print(f"\nMLflow Run ID: {run_id}")
    print(f"Model URI: {model_info.model_uri}")

# COMMAND ----------

# DBTITLE 1,Register model in Unity Catalog
# Register the model to Unity Catalog
registered_model_name = REGISTERED_MODEL_NAME

result = mlflow.register_model(
    model_uri=model_info.model_uri,
    name=registered_model_name
)

print(f"Registered model: {registered_model_name}")
print(f"Version: {result.version}")
print(f"Status: {result.status}")

# Set alias for serving
from mlflow import MlflowClient
client = MlflowClient()
client.set_registered_model_alias(
    name=registered_model_name,
    alias="champion",
    version=result.version
)
print(f"Alias 'champion' set to version {result.version}")

# COMMAND ----------

# DBTITLE 1,Deploy classic model serving endpoint
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import (
    EndpointCoreConfigInput,
    ServedEntityInput,
)
import time

w = WorkspaceClient()

endpoint_name = ENDPOINT_NAME
model_name = REGISTERED_MODEL_NAME
model_version = result.version

print(f"Deploying endpoint: {endpoint_name}")
print(f"Model: {model_name} v{model_version}")

# Create or update the endpoint
try:
    # Try to get existing endpoint
    existing = w.serving_endpoints.get(endpoint_name)
    print(f"Endpoint exists, updating...")
    w.serving_endpoints.update_config_and_wait(
        name=endpoint_name,
        served_entities=[
            ServedEntityInput(
                entity_name=model_name,
                entity_version=str(model_version),
                workload_size="Small",
                scale_to_zero_enabled=True,
            )
        ],
    )
except Exception as e:
    if "RESOURCE_DOES_NOT_EXIST" in str(e) or "does not exist" in str(e).lower():
        print("Creating new endpoint...")
        w.serving_endpoints.create_and_wait(
            name=endpoint_name,
            config=EndpointCoreConfigInput(
                served_entities=[
                    ServedEntityInput(
                        entity_name=model_name,
                        entity_version=str(model_version),
                        workload_size="Small",
                        scale_to_zero_enabled=True,
                    )
                ],
            ),
        )
    else:
        raise e

# Get endpoint status
endpoint = w.serving_endpoints.get(endpoint_name)
print(f"\n{'='*60}")
print(f"ENDPOINT DEPLOYED SUCCESSFULLY")
print(f"{'='*60}")
print(f"Name: {endpoint.name}")
print(f"State: {endpoint.state.ready}")
print(f"URL: https://{w.config.host}/serving-endpoints/{endpoint_name}/invocations")
print(f"Model: {model_name} v{model_version}")
print(f"Scale-to-zero: enabled")
print(f"Workload size: Small")

# COMMAND ----------

# DBTITLE 1,Test endpoint with sample payload
# Test the endpoint with a sample member-action pair
import json
import requests

# Build sample payload from training data (3 rows)
sample_records = X_train.iloc[:3].to_dict(orient='records')

print("Testing endpoint with 3 sample member-action pairs...")
print(f"Endpoint: https://{w.config.host}/serving-endpoints/{endpoint_name}/invocations")

# Query via REST API for reliable test
host = w.config.host.replace("https://", "").replace("http://", "")
token = w.config.authenticate()

resp = requests.post(
    f"https://{host}/serving-endpoints/{endpoint_name}/invocations",
    headers={**token, "Content-Type": "application/json"},
    json={"dataframe_records": sample_records},
    timeout=120
)

if resp.status_code == 200:
    predictions = resp.json().get("predictions", [])
    print(f"\n✅ Endpoint is live and scoring!")
    print(f"   Predictions for 3 sample rows: {[round(p, 4) for p in predictions]}")
    print(f"\n   These are relevance scores (0-1) for member-action pairs.")
    print(f"   Higher = stronger recommendation.")
else:
    print(f"\n⚠️  Response code: {resp.status_code}")
    print(f"   {resp.text[:200]}")
    if resp.status_code == 503:
        print(f"   Endpoint is scaling up (scale-to-zero). Retry in ~60s.")