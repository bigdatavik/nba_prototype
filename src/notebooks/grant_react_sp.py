# Databricks notebook source
# DBTITLE 1,Overview
# MAGIC %md
# MAGIC ## Grant Lakebase access to a second app service principal (React console)
# MAGIC
# MAGIC **Non-destructive.** No branch re-fork, no dropped tables, no reseed. Ensures a
# MAGIC Postgres LOGIN role exists for the given app service principal and grants it read
# MAGIC (production) / read+write (app-writes) on the NBA schema — the same grants
# MAGIC `nba_bootstrap` gives the original app SP, applied to a second SP so the React app
# MAGIC (`app_name_react`) coexists with the Streamlit app. Idempotent; safe to re-run.
# MAGIC Runs as the notebook user (Lakebase project owner), who can grant on the tables.

# COMMAND ----------

# DBTITLE 1,Install deps (%pip auto-restarts Python on serverless — no manual restart)
# MAGIC %pip install psycopg2-binary "databricks-sdk>=0.118.0" --quiet

# COMMAND ----------

# DBTITLE 1,Widgets / config (nothing hardcoded)
dbutils.widgets.text("lakebase_project", "", "Lakebase project id")
dbutils.widgets.text("lakebase_database", "databricks_postgres", "Lakebase database (SQL dbname)")
dbutils.widgets.text("lakebase_schema", "nba_new_lbase", "Lakebase (Postgres) schema")
dbutils.widgets.text("lakebase_branch", "production", "Production branch (read path)")
dbutils.widgets.text("app_writes_branch", "app-writes", "App-writes branch (CRUD path)")
dbutils.widgets.text("react_app_name", "", "React app name (for SP auto-resolve)")
dbutils.widgets.text("react_sp", "", "React app SP client id (explicit; blank = auto-resolve)")

LAKEBASE_PROJECT = dbutils.widgets.get("lakebase_project")
LAKEBASE_DATABASE = dbutils.widgets.get("lakebase_database")
LAKEBASE_SCHEMA = dbutils.widgets.get("lakebase_schema")
PROD_BRANCH = dbutils.widgets.get("lakebase_branch")
APP_WRITES_BRANCH = dbutils.widgets.get("app_writes_branch")
REACT_APP_NAME = dbutils.widgets.get("react_app_name").strip()
REACT_SP = dbutils.widgets.get("react_sp").strip()
assert LAKEBASE_PROJECT, "lakebase_project is required"

# COMMAND ----------

# DBTITLE 1,Resolve identity + the SP to grant, and run all grants
import json
import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
db_user = w.current_user.me().user_name
summary = {"grantor": db_user, "sp": None, "branches": {}}

if not REACT_SP:
    for _app in w.apps.list():
        if (REACT_APP_NAME and _app.name == REACT_APP_NAME) or \
           (not REACT_APP_NAME and "react" in (_app.name or "").lower()):
            REACT_SP = _app.service_principal_client_id or ""
            break
assert REACT_SP, "Could not resolve the React app service principal (set react_sp or react_app_name)."
summary["sp"] = REACT_SP


def connect(branch: str):
    endpoint = f"projects/{LAKEBASE_PROJECT}/branches/{branch}/endpoints/primary"
    host = w.postgres.get_endpoint(name=endpoint).status.hosts.host
    token = w.postgres.generate_database_credential(endpoint=endpoint).token
    c = psycopg2.connect(host=host, port=5432, dbname=LAKEBASE_DATABASE,
                         user=db_user, password=token, sslmode="require")
    c.autocommit = True
    return c, host


def ensure_role(cur):
    try:
        cur.execute(f'CREATE ROLE "{REACT_SP}" LOGIN')
    except Exception:
        pass  # already exists


def grant_all_tables(cur, privileges: str):
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
                (LAKEBASE_SCHEMA,))
    ok, skipped = [], {}
    for (t,) in cur.fetchall():
        try:
            cur.execute(f'GRANT {privileges} ON {LAKEBASE_SCHEMA}."{t}" TO "{REACT_SP}"')
            ok.append(t)
        except Exception as e:
            skipped[t] = str(e).splitlines()[0]
    return ok, skipped


def superuser(cur):
    cur.execute("SHOW is_superuser")
    return cur.fetchone()[0]


# --- PRODUCTION (read) ---
c, host = connect(PROD_BRANCH)
cur = c.cursor()
info = {"host": host, "is_superuser": superuser(cur)}
ensure_role(cur)
cur.execute(f'GRANT USAGE ON SCHEMA {LAKEBASE_SCHEMA} TO "{REACT_SP}"')
info["granted"], info["skipped"] = grant_all_tables(cur, "SELECT")
summary["branches"][PROD_BRANCH] = info
c.close()

# --- APP-WRITES (read + write) ---
c, host = connect(APP_WRITES_BRANCH)
cur = c.cursor()
info = {"host": host, "is_superuser": superuser(cur)}
ensure_role(cur)
cur.execute(f'GRANT USAGE, CREATE ON SCHEMA {LAKEBASE_SCHEMA} TO "{REACT_SP}"')
info["granted"], info["skipped"] = grant_all_tables(cur, "SELECT, INSERT, UPDATE, DELETE")
try:
    cur.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {LAKEBASE_SCHEMA} TO "{REACT_SP}"')
    info["sequences"] = "ok"
except Exception as e:
    info["sequences"] = str(e).splitlines()[0]
try:
    cur.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA {LAKEBASE_SCHEMA} '
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{REACT_SP}"')
    info["default_privileges"] = "ok"
except Exception as e:
    info["default_privileges"] = str(e).splitlines()[0]
summary["branches"][APP_WRITES_BRANCH] = info
c.close()

print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
