"""
Lakebase connectivity + data access.

Copied (behavior-preserving) from src/app/app.py. The only change vs. the
Streamlit original is that Streamlit UI calls (st.error/st.info/...) are replaced
with module logging — the auth path, host resolution, self-heal-on-re-fork retry,
and SQL are unchanged.

Host is resolved dynamically via the SDK (w.postgres.get_endpoint) and self-heals
on connection failure; credentials come from the Lakebase credentials REST API
using the app SP's injected auth (Config().authenticate()).
"""

import time
import logging
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
import requests

from . import config
from .config import (
    LAKEBASE_DATABASE,
    LAKEBASE_SCHEMA,
    PRODUCTION_ENDPOINT,
    APP_WRITES_ENDPOINT,
    PRODUCTION_HOST_OVERRIDE,
    APP_WRITES_HOST_OVERRIDE,
    get_databricks_host,
)

log = logging.getLogger("nba.db")

# Resolved-host caches (one per branch). Cleared on connection failure so the
# next call re-resolves the current host after a branch re-fork.
_prod_host_cache = {"host": PRODUCTION_HOST_OVERRIDE or None}
_app_writes_host_cache = {"host": APP_WRITES_HOST_OVERRIDE or None}

# Credential caches (one per branch)
_cred_cache_prod = {"token": None, "user": None, "expires": 0}
_cred_cache_writes = {"token": None, "user": None, "expires": 0}


def _resolve_host(endpoint: str, cache: dict, override: str) -> Optional[str]:
    """Resolve the current Postgres host for an endpoint from the SDK. Caches
    until a connection failure invalidates it. Falls back to an explicit host
    override env var if the SDK lookup fails."""
    if cache["host"]:
        return cache["host"]
    try:
        from databricks.sdk import WorkspaceClient
        _w = WorkspaceClient()
        ep = _w.postgres.get_endpoint(name=endpoint)
        cache["host"] = ep.status.hosts.host
    except Exception:
        cache["host"] = override or None
    return cache["host"]


def _resolve_production_host():
    return _resolve_host(PRODUCTION_ENDPOINT, _prod_host_cache, PRODUCTION_HOST_OVERRIDE)


def _resolve_app_writes_host():
    return _resolve_host(APP_WRITES_ENDPOINT, _app_writes_host_cache, APP_WRITES_HOST_OVERRIDE)


def _invalidate_production_host():
    _prod_host_cache["host"] = None


def _invalidate_app_writes_host():
    _app_writes_host_cache["host"] = None


# =============================================================================
# Lakebase Connection
# =============================================================================

def _get_credential(endpoint: str, cache: dict) -> tuple:
    """Generate Lakebase credential for a specific branch endpoint."""
    if cache["expires"] > time.time():
        return cache["token"], cache["user"]

    from databricks.sdk.core import Config
    cfg = Config()
    host = get_databricks_host()
    auth_headers = cfg.authenticate()
    auth_headers["Content-Type"] = "application/json"

    resp = requests.post(
        f"https://{host}/api/2.0/postgres/credentials",
        headers=auth_headers,
        json={"endpoint": endpoint},
        timeout=15,
    )
    if resp.status_code != 200:
        log.error("Lakebase credential error (%s): %s", endpoint, resp.status_code)
        return None, None

    data = resp.json()
    token = data.get("token") or data.get("password")
    user = data.get("username") or data.get("user")
    cache.update({"token": token, "user": user, "expires": time.time() + 2400})
    return token, user


def get_production_connection():
    """Connection to PRODUCTION branch — member_features (read-only, always fresh).
    Host is resolved dynamically via the SDK and self-heals if it changes."""
    global _cred_cache_prod
    token, user = _get_credential(PRODUCTION_ENDPOINT, _cred_cache_prod)
    if not token:
        return None
    try:
        conn = psycopg2.connect(
            host=_resolve_production_host(),
            port=5432,
            dbname=LAKEBASE_DATABASE,
            user=user,
            password=token,
            sslmode="require",
            connect_timeout=10,
        )
        conn.autocommit = True
        return conn
    except psycopg2.OperationalError:
        # Host changed — clear cache, resolve fresh, retry once
        _invalidate_production_host()
        token, user = _get_credential(PRODUCTION_ENDPOINT, _cred_cache_prod)
        if not token:
            return None
        try:
            conn = psycopg2.connect(
                host=_resolve_production_host(),
                port=5432,
                dbname=LAKEBASE_DATABASE,
                user=user,
                password=token,
                sslmode="require",
                connect_timeout=10,
            )
            conn.autocommit = True
            return conn
        except Exception as e:
            log.error("Production connection failed after re-resolve: %s", e)
            return None


def get_app_writes_connection():
    """Connection to APP-WRITES branch — self-heals after branch re-fork."""
    global _cred_cache_writes
    token, user = _get_credential(APP_WRITES_ENDPOINT, _cred_cache_writes)
    if not token:
        return None
    try:
        conn = psycopg2.connect(
            host=_resolve_app_writes_host(),
            port=5432,
            dbname=LAKEBASE_DATABASE,
            user=user,
            password=token,
            sslmode="require",
            connect_timeout=10,
        )
        conn.autocommit = True
        return conn
    except psycopg2.OperationalError:
        # Host dead after re-fork — clear cache, resolve fresh, retry once
        _invalidate_app_writes_host()
        token, user = _get_credential(APP_WRITES_ENDPOINT, _cred_cache_writes)
        if not token:
            return None
        try:
            conn = psycopg2.connect(
                host=_resolve_app_writes_host(),
                port=5432,
                dbname=LAKEBASE_DATABASE,
                user=user,
                password=token,
                sslmode="require",
                connect_timeout=10,
            )
            conn.autocommit = True
            return conn
        except Exception as e:
            log.error("Connection failed after re-resolve: %s", e)
            return None


# =============================================================================
# Data Access (Lakebase)
# =============================================================================

def get_all_members() -> list:
    """Get list of all member IDs from PRODUCTION branch."""
    conn = get_production_connection()
    if not conn:
        return []
    with conn.cursor() as cur:
        cur.execute(f"SELECT member_id FROM {LAKEBASE_SCHEMA}.member_features ORDER BY member_id")
        return [row[0] for row in cur.fetchall()]


def get_member_features(member_id: str) -> Optional[dict]:
    """Fetch member features from PRODUCTION branch (always fresh from UC sync)."""
    conn = get_production_connection()
    if not conn:
        return None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT * FROM {LAKEBASE_SCHEMA}.member_features WHERE member_id = %s",
            (member_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_action_catalog() -> list:
    """Fetch actions from PRODUCTION branch (only reconciled/approved changes)."""
    conn = get_production_connection()
    if not conn:
        return []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {LAKEBASE_SCHEMA}.action_catalog ORDER BY value_score DESC")
        return [dict(row) for row in cur.fetchall()]


def get_action_catalog_staged() -> list:
    """Fetch actions from APP-WRITES branch (includes pending CRUD changes)."""
    conn = get_app_writes_connection()
    if not conn:
        return []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {LAKEBASE_SCHEMA}.action_catalog ORDER BY value_score DESC")
        return [dict(row) for row in cur.fetchall()]


# =============================================================================
# CRUD Operations (for Manage Actions page)
# =============================================================================

def add_action(action_data: dict) -> bool:
    conn = get_app_writes_connection()
    if not conn:
        return False
    cols = list(action_data.keys())
    vals = [action_data[c] for c in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    col_names = ", ".join(cols)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO {LAKEBASE_SCHEMA}.action_catalog ({col_names}) VALUES ({placeholders})", vals)
    return True


def update_action(action_id: str, updates: dict) -> bool:
    conn = get_app_writes_connection()
    if not conn:
        return False
    set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
    vals = list(updates.values()) + [action_id]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {LAKEBASE_SCHEMA}.action_catalog SET {set_clause} WHERE action_id = %s", vals)
    return True


def delete_action(action_id: str) -> bool:
    conn = get_app_writes_connection()
    if not conn:
        return False
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {LAKEBASE_SCHEMA}.action_catalog WHERE action_id = %s", (action_id,))
    return True


def get_change_log() -> list:
    """Legacy audit rows from the app-writes trigger table. Returns JSON records
    (the Streamlit version returned a DataFrame)."""
    conn = get_app_writes_connection()
    if not conn:
        return []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {LAKEBASE_SCHEMA}._action_catalog_changes ORDER BY changed_at DESC LIMIT 50")
        rows = cur.fetchall()
    return [dict(r) for r in rows] if rows else []


# =============================================================================
# Decisions — Approve & Act (closed loop)
# =============================================================================

DECISIONS_TABLE = "nba_decisions"
_DECISIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {LAKEBASE_SCHEMA}.{DECISIONS_TABLE} (
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
"""


def ensure_decisions_table() -> bool:
    """Best-effort create of the decisions table (see Streamlit original)."""
    conn = get_app_writes_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_DECISIONS_DDL)
    except Exception:
        pass  # already exists, or no CREATE — reads/writes will report if needed
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {LAKEBASE_SCHEMA}.{DECISIONS_TABLE} REPLICA IDENTITY FULL")
    except Exception:
        pass
    return True


def record_decision(member_id, action_id, action_name, channel, score,
                    status, disposition, note, approver) -> bool:
    ensure_decisions_table()
    conn = get_app_writes_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {LAKEBASE_SCHEMA}.{DECISIONS_TABLE}
                    (member_id, action_id, action_name, channel, recommended_score,
                     status, disposition, note, approver)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (member_id, action_id, action_name, channel, float(score or 0),
                 status, disposition, note or None, approver or None),
            )
        return True
    except Exception as e:
        log.error("Could not write decision (app SP may lack CREATE/INSERT on %s): %s",
                  LAKEBASE_SCHEMA, e)
        return False


def get_decisions(member_id: Optional[str] = None, limit: int = 100) -> list:
    ensure_decisions_table()
    conn = get_app_writes_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if member_id:
                cur.execute(
                    f"SELECT * FROM {LAKEBASE_SCHEMA}.{DECISIONS_TABLE} "
                    f"WHERE member_id = %s ORDER BY created_at DESC LIMIT %s",
                    (member_id, limit))
            else:
                cur.execute(
                    f"SELECT * FROM {LAKEBASE_SCHEMA}.{DECISIONS_TABLE} "
                    f"ORDER BY created_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def update_decision_outcome(decision_id, outcome: str) -> bool:
    conn = get_app_writes_connection()
    if not conn:
        return False
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {LAKEBASE_SCHEMA}.{DECISIONS_TABLE} SET outcome = %s "
            f"WHERE decision_id = %s", (outcome, decision_id))
    return True
