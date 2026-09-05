"""
Healthcare Payer Next-Best-Action Console
Reads member features from Lakebase, scores via Model Serving, displays NBA.
"""

import os
import time
import json
from datetime import datetime
from typing import Optional

import streamlit as st
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
import requests

# =============================================================================
# Configuration — Dual Branch Architecture (fully environment-driven)
# =============================================================================
# PRODUCTION branch: member_features (read-only, synced daily from UC)
# APP-WRITES branch: action_catalog (CRUD) + audit trail
#
# All values below come from environment variables set in app.yaml, which in
# turn are populated per-target by the Databricks Asset Bundle. Nothing here is
# hardcoded to a specific workspace, Lakebase project, or endpoint host — the
# same image runs in dev, prod, or any customer environment by changing config.
#
# Lakebase endpoints are expressed as their resource paths:
#   projects/<project>/branches/<branch>/endpoints/primary
# and the actual Postgres host is resolved at runtime via the SDK. This makes
# the app self-healing across branch re-forks (host changes) with no redeploy.

LAKEBASE_DATABASE = os.getenv("LAKEBASE_DATABASE", "databricks_postgres")
LAKEBASE_SCHEMA = os.getenv("LAKEBASE_SCHEMA", "nba_new_lbase")
MODEL_ENDPOINT_NAME = os.getenv("MODEL_ENDPOINT_NAME", "nba-scoring-endpoint")

# Optional feature env vars are "disabled" when blank. The DABs Apps deploy API
# rejects an env var whose value is an empty string, so the bundle passes a
# non-empty sentinel ("-") for unset optional vars; treat the sentinel (and any
# whitespace) as blank here so the feature stays cleanly disabled.
_DISABLED_SENTINELS = {"", "-", "none", "disabled"}


def _optional_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    return "" if v.lower() in _DISABLED_SENTINELS else v


# Genie Space for the "Ask NBA" page (natural-language analytics over UC). Blank
# disables the page. The space is bound to its own SQL warehouse at creation, so
# the app only needs the space id.
GENIE_SPACE_ID = _optional_env("GENIE_SPACE_ID")

# Foundation Model (chat) endpoint used by the per-member "Assist" — drafts the
# outreach message. Blank disables the draft feature (reason codes + what-if
# still work). Any Databricks FM chat endpoint (llm/v1/chat) works.
LLM_ENDPOINT_NAME = _optional_env("LLM_ENDPOINT_NAME")

# Lakebase project + branch names → endpoint resource paths.
# Prefer the explicit endpoint env vars if provided; otherwise build them from
# project/branch so callers only need to set two simple values.
LAKEBASE_PROJECT = os.getenv("LAKEBASE_PROJECT", "")
LAKEBASE_BRANCH_PRODUCTION = os.getenv("LAKEBASE_BRANCH_PRODUCTION", "production")
LAKEBASE_BRANCH_APP_WRITES = os.getenv("LAKEBASE_BRANCH_APP_WRITES", "app-writes")


def _endpoint_path(branch: str) -> str:
    return f"projects/{LAKEBASE_PROJECT}/branches/{branch}/endpoints/primary"


PRODUCTION_ENDPOINT = os.getenv(
    "LAKEBASE_ENDPOINT_PRODUCTION", _endpoint_path(LAKEBASE_BRANCH_PRODUCTION)
)
APP_WRITES_ENDPOINT = os.getenv(
    "LAKEBASE_ENDPOINT_APP_WRITES", _endpoint_path(LAKEBASE_BRANCH_APP_WRITES)
)

# Optional static host overrides (rarely needed — hosts are resolved via SDK).
# Leave unset to let the app resolve/self-heal hosts dynamically.
PRODUCTION_HOST_OVERRIDE = os.getenv("LAKEBASE_HOST_PRODUCTION", "")
APP_WRITES_HOST_OVERRIDE = os.getenv("LAKEBASE_HOST_APP_WRITES", "")

# Resolved-host caches (one per branch). Cleared on connection failure so the
# next call re-resolves the current host after a branch re-fork.
_prod_host_cache = {"host": PRODUCTION_HOST_OVERRIDE or None}
_app_writes_host_cache = {"host": APP_WRITES_HOST_OVERRIDE or None}


def _resolve_host(endpoint: str, cache: dict, override: str) -> Optional[str]:
    """Resolve the current Postgres host for an endpoint from the SDK.

    Caches until a connection failure invalidates it. Falls back to an explicit
    host override env var if the SDK lookup fails.
    """
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
    """Clear cached prod host so next call re-resolves (called on connection failure)."""
    _prod_host_cache["host"] = None


def _invalidate_app_writes_host():
    """Clear cached host so next call re-resolves (called on connection failure)."""
    _app_writes_host_cache["host"] = None


# Credential caches (one per branch)
_cred_cache_prod = {"token": None, "user": None, "expires": 0}
_cred_cache_writes = {"token": None, "user": None, "expires": 0}


# =============================================================================
# Lakebase Connection
# =============================================================================

def get_databricks_host() -> str:
    """Resolve the workspace host from the SDK config (env DATABRICKS_HOST or
    the app's injected credentials). No workspace is hardcoded."""
    try:
        from databricks.sdk.core import Config
        cfg = Config()
        host = (cfg.host or "").replace("https://", "").replace("http://", "").rstrip("/")
        if host:
            return host
    except Exception:
        pass
    return os.getenv("DATABRICKS_HOST", "").replace("https://", "").replace("http://", "").rstrip("/")


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
        st.error(f"Lakebase credential error ({endpoint}): {resp.status_code}")
        return None, None

    data = resp.json()
    token = data.get("token") or data.get("password")
    user = data.get("username") or data.get("user")
    cache.update({"token": token, "user": user, "expires": time.time() + 2400})
    return token, user


def get_production_connection():
    """Connection to PRODUCTION branch — member_features (read-only, always fresh).

    Host is resolved dynamically via the SDK and self-heals if it changes.
    """
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
            st.error(f"Production connection failed after re-resolve: {e}")
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
            st.error(f"Connection failed after re-resolve: {e}")
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
# Model Scoring
# =============================================================================

def score_actions(member_features: dict, actions: list) -> list:
    """Call nba-scoring-endpoint to score each action for this member."""
    host = get_databricks_host()
    from databricks.sdk.core import Config
    cfg = Config()
    auth_headers = cfg.authenticate()
    auth_headers["Content-Type"] = "application/json"

    # Member feature columns (must match model training)
    m_cols = [
        'age', 'is_male', 'is_dual_eligible_flag', 'total_claims_12m',
        'total_paid_amount_12m', 'avg_claim_amount', 'high_cost_claims_12m',
        'preventive_visits_12m', 'chronic_claims_12m', 'total_interactions_12m',
        'negative_interactions_12m', 'complaints_12m', 'escalations_12m',
        'avg_satisfaction_score', 'phone_interactions', 'digital_interactions',
        'campaigns_received_12m', 'response_rate', 'digital_sessions_12m',
        'churn_risk_score', 'engagement_score', 'escalation_likelihood',
        'care_outreach_likelihood', 'plan_switch_propensity', 'raf_score',
        'clinical_risk_score', 'predicted_cost_12m', 'has_preventive_gap',
        'is_care_mgmt_candidate', 'is_churn_risk',
    ]
    a_cols = ['value_score', 'strategic_priority', 'compliance_flag', 'min_spacing_days']

    # Build payload: one row per action
    records = []
    for action in actions:
        row = {}
        for col in m_cols:
            val = member_features.get(col, 0)
            row[f"m_{col}"] = float(val) if val is not None else 0.0
        row["a_value_score"] = float(action.get("value_score", 0))
        row["a_strategic_priority"] = float(action.get("strategic_priority", 3))
        row["a_compliance_flag"] = 1.0 if action.get("compliance_flag") else 0.0
        row["a_min_spacing_days"] = float(action.get("min_spacing_days", 30))
        records.append(row)

    # Call endpoint (with retry for cold-start wake-up)
    endpoint_url = f"https://{host}/serving-endpoints/{MODEL_ENDPOINT_NAME}/invocations"
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            resp = requests.post(
                endpoint_url,
                headers=auth_headers,
                json={"dataframe_records": records},
                timeout=120,  # 2 min to allow cold-start wake-up
            )
            if resp.status_code == 200:
                return resp.json().get("predictions", [])
            elif resp.status_code == 503:
                # Endpoint waking up — retry
                if attempt < max_attempts - 1:
                    st.info(f"\u23f3 Model endpoint is waking up (scale-to-zero)... retry {attempt + 2}/{max_attempts}")
                    time.sleep(5)
                    continue
            else:
                st.warning(f"Model endpoint returned {resp.status_code}. Using value_score as fallback.")
                return [a["value_score"] / 100.0 for a in actions]
        except requests.exceptions.ReadTimeout:
            if attempt < max_attempts - 1:
                st.info(f"\u23f3 Model endpoint is waking up from cold start (~60s)... retry {attempt + 2}/{max_attempts}")
                continue
            else:
                st.warning("Model endpoint timed out after retries. Using value_score as fallback.")
                return [a["value_score"] / 100.0 for a in actions]

    st.warning("Model endpoint unavailable. Using value_score as fallback.")
    return [a["value_score"] / 100.0 for a in actions]


# =============================================================================
# Orchestration Rules
# =============================================================================

def select_channel(member_features: dict, action: dict) -> str:
    """Select optimal channel based on member preference and action eligibility."""
    eligible = action.get("eligible_channels", [])
    if isinstance(eligible, str):
        eligible = json.loads(eligible)

    # Channel affinity from member features
    digital = float(member_features.get("digital_interactions", 0) or 0)
    phone = float(member_features.get("phone_interactions", 0) or 0)

    if digital > phone and "Digital" in eligible:
        return "Digital"
    elif phone > digital and "Call center" in eligible:
        return "Call center"
    elif "Provider" in eligible:
        return "Provider"
    elif eligible:
        return eligible[0]
    return "Digital"


def apply_orchestration(member_features: dict, actions: list, scores: list) -> list:
    """Apply suppression, timing, spacing rules and rank actions."""
    results = []
    for i, action in enumerate(actions):
        score = scores[i] if i < len(scores) else 0
        channel = select_channel(member_features, action)

        results.append({
            "rank": 0,
            "action_id": action["action_id"],
            "action_name": action["action_name"],
            "category": action.get("action_category", ""),
            "team": action.get("team_owner", ""),
            "score": round(score, 4),
            "channel": channel,
            "value_score": action.get("value_score", 0),
            "compliance": action.get("compliance_flag", False),
            "description": action.get("description", ""),
        })

    # Sort by model score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    return results


# =============================================================================
# Assist — per-member decision support (explain, draft, what-if)
# =============================================================================
# Turns Member Lookup from a lookup into a decision: WHY this action, a drafted
# outreach message, and a what-if re-score. Reason codes are deterministic (no
# model call); the draft uses a Foundation Model chat endpoint.

def _f(member: dict, key: str, default=0.0) -> float:
    """Safe float accessor for member features."""
    val = member.get(key, default)
    try:
        return float(val) if val is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def explain_recommendation(member: dict, action: dict) -> list:
    """Deterministic, human-readable reasons this action was recommended for
    this member — derived from member features + the action's attributes."""
    reasons = []
    cat = (action.get("action_category") or "").upper()

    if _f(member, "has_preventive_gap") >= 1 and cat == "STARS":
        reasons.append("Open preventive / Stars care gap — this action targets it.")
    if (_f(member, "is_care_mgmt_candidate") >= 1 or _f(member, "clinical_risk_score") >= 0.7) \
            and cat in ("PCO", "HOME HEALTH", "MRA"):
        reasons.append(f"High clinical risk (score {_f(member,'clinical_risk_score'):.2f}) — care-management candidate.")
    if _f(member, "chronic_claims_12m") >= 5:
        reasons.append(f"Elevated chronic-condition claims ({int(_f(member,'chronic_claims_12m'))} in 12m).")
    if (_f(member, "is_churn_risk") >= 1 or _f(member, "churn_risk_score") >= 0.5) and cat == "PCO":
        reasons.append(f"Elevated churn risk (score {_f(member,'churn_risk_score'):.2f}) — retention-oriented.")
    if _f(member, "raf_score") >= 1.8 and cat == "MRA":
        reasons.append(f"RAF recapture opportunity (RAF {_f(member,'raf_score'):.2f}).")
    if action.get("compliance_flag"):
        reasons.append("Regulatory / compliance action — prioritized and not suppressible.")
    if _f(member, "value_score", action.get("value_score", 0)) and int(action.get("value_score", 0)) >= 85:
        reasons.append(f"High strategic value (value score {int(action.get('value_score',0))}).")
    if int(action.get("strategic_priority", 5)) == 1:
        reasons.append("Top strategic priority (P1).")

    digital = _f(member, "digital_interactions")
    phone = _f(member, "phone_interactions")
    if digital > phone:
        reasons.append("Member engages more via digital — matches the recommended channel.")
    elif phone > digital:
        reasons.append("Member engages more by phone — matches the recommended channel.")
    if _f(member, "engagement_score") <= 4:
        reasons.append(f"Low engagement ({_f(member,'engagement_score'):.1f}/10) — proactive outreach may re-activate.")

    if not reasons:
        reasons.append(f"Best model fit for this member among available actions "
                       f"(value score {int(action.get('value_score',0))}).")
    return reasons[:5]


def priority_band(score: float) -> str:
    """Map an NBA priority score (0-1) to a band label."""
    if score >= 0.66:
        return "High priority"
    if score >= 0.40:
        return "Medium priority"
    return "Watch"


def project_trajectory(member: dict, action: dict, score: float) -> dict:
    """Heuristic projection of the member's risk with vs. without this action.
    Illustrative (not a clinical prediction) — scales the expected lift by the
    action's value_score and uses the risk signal that fits the action category."""
    cat = (action.get("action_category") or "").upper()
    churn = _f(member, "churn_risk_score")
    clinical = _f(member, "clinical_risk_score")
    if cat == "PCO":
        current, label = churn, "churn risk"
    elif cat in ("MRA", "HOME HEALTH"):
        current, label = clinical, "clinical risk"
    else:  # STARS / Pharmacy / other → clinical/care-gap proxy
        current, label = max(clinical, churn), "risk"
    value = float(action.get("value_score", 70) or 70)
    lift = min(0.45, 0.15 + value / 300.0)          # 15%–45% expected reduction
    without = min(0.99, round(current + 0.05, 2))    # drifts up without action
    with_new = round(current * (1 - lift), 2)
    return {"label": label, "current": round(current, 2), "without": without,
            "with_new": with_new, "lift_pct": round(lift * 100)}


PRIORITY_THRESHOLD = 0.66


def call_llm(messages: list, max_tokens: int = 300, temperature: float = 0.4) -> Optional[str]:
    """Call a Databricks Foundation Model chat endpoint. Returns text or None."""
    if not LLM_ENDPOINT_NAME:
        return None
    host = get_databricks_host()
    from databricks.sdk.core import Config
    cfg = Config()
    headers = cfg.authenticate()
    headers["Content-Type"] = "application/json"
    try:
        resp = requests.post(
            f"https://{host}/serving-endpoints/{LLM_ENDPOINT_NAME}/invocations",
            headers=headers,
            json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=60,
        )
        if resp.status_code != 200:
            return None
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def draft_outreach(member: dict, action: dict, channel: str) -> Optional[str]:
    """Draft a short, compliant member outreach message for this action/channel."""
    age = int(_f(member, "age"))
    context = (
        f"Member context (no PHI): age band ~{age}, engagement {_f(member,'engagement_score'):.1f}/10, "
        f"preferred channel {channel}."
    )
    system = (
        "You are a healthcare payer member-engagement assistant. Write a short, warm, "
        "plain-language outreach message. Be compliant: no PHI, no diagnosis claims, no "
        "guarantees; include a clear next step. Keep it under 80 words."
    )
    user = (
        f"Action: {action.get('action_name')} — {action.get('description','')}\n"
        f"Channel: {channel}\n{context}\n\n"
        f"Write the {channel} outreach message."
    )
    return call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}])


# =============================================================================
# CRUD Operations (for Manage Actions page)
# =============================================================================

CATEGORIES = ["STARS", "MRA", "Pharmacy", "PCO", "Home health", "Regulatory"]
TEAMS = ["Clinical innovation", "Stars", "Pharmacy", "PCO", "Home health", "Regulatory", "MRA"]
CHANNELS = ["Digital", "Call center", "Provider", "E/Mail"]


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


def get_change_log() -> pd.DataFrame:
    conn = get_app_writes_connection()
    if not conn:
        return pd.DataFrame()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {LAKEBASE_SCHEMA}._action_catalog_changes ORDER BY changed_at DESC LIMIT 50")
        rows = cur.fetchall()
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


# =============================================================================
# Decisions — Approve & Act (closed loop)
# =============================================================================
# Committed NBA decisions are written to the APP-WRITES branch (writable
# Postgres — never the read-only synced tables), and read back on the next
# request so the loop closes. The table is created lazily (the app SP has
# CAN_CONNECT_AND_CREATE on app-writes).

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
    """Best-effort create of the decisions table. The table is normally created
    with the SP's CREATE grant (bootstrap); if CREATE isn't permitted we swallow
    it and let read/write surface any real issue (the table may already exist)."""
    conn = get_app_writes_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_DECISIONS_DDL)
    except Exception:
        pass  # already exists, or no CREATE — reads/writes will report if needed
    # Make the table CDF-ready (full before-images). Only the owner can; when the
    # table was pre-created by bootstrap this no-ops, which is fine.
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
        st.error(f"Could not write decision (app SP may lack CREATE/INSERT on "
                 f"{LAKEBASE_SCHEMA}): {e}")
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


def get_app_user() -> str:
    """Best-effort end-user email forwarded by Databricks Apps (else blank)."""
    try:
        ctx = getattr(st, "context", None)
        if ctx is not None and getattr(ctx, "headers", None):
            return ctx.headers.get("X-Forwarded-Email") or ""
    except Exception:
        pass
    return ""


# =============================================================================
# Genie Conversation API (Ask NBA page)
# =============================================================================
# The app calls the Genie Conversation API with its own injected credentials
# (same auth path as score_actions). Genie reads Unity Catalog directly through
# its bound SQL warehouse, so this never touches the Lakebase read/write path.

def _genie_headers():
    from databricks.sdk.core import Config
    cfg = Config()
    h = cfg.authenticate()
    h["Content-Type"] = "application/json"
    return h


def genie_ask(question: str, conversation_id: Optional[str] = None) -> dict:
    """Ask the Genie space a question (start or continue a conversation) and wait
    for the answer. Returns {conversation_id, answer, sql, dataframe, error}."""
    host = get_databricks_host()
    base = f"https://{host}/api/2.0/genie/spaces/{GENIE_SPACE_ID}"
    headers = _genie_headers()
    out = {"conversation_id": conversation_id, "answer": "", "sql": "",
           "dataframe": None, "error": None}
    try:
        # Start or continue the conversation
        if conversation_id:
            r = requests.post(f"{base}/conversations/{conversation_id}/messages",
                              headers=headers, json={"content": question}, timeout=30)
        else:
            r = requests.post(f"{base}/start-conversation",
                              headers=headers, json={"content": question}, timeout=30)
        if r.status_code != 200:
            out["error"] = f"Genie start error {r.status_code}: {r.text[:300]}"
            return out
        data = r.json()
        cid = data.get("conversation_id") or conversation_id
        mid = data.get("message_id") or (data.get("message") or {}).get("id")
        out["conversation_id"] = cid

        # Poll the message until it completes (Genie + warehouse can take a bit)
        msg = {}
        for _ in range(40):  # ~200s max
            m = requests.get(f"{base}/conversations/{cid}/messages/{mid}",
                             headers=headers, timeout=30)
            if m.status_code != 200:
                out["error"] = f"Genie poll error {m.status_code}"
                return out
            msg = m.json()
            status = msg.get("status")
            if status in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
                break
            time.sleep(5)

        if msg.get("status") != "COMPLETED":
            # Surface the real reason: Genie puts failure detail in `error`, and
            # sometimes inside an attachment. Fall back to a compact JSON dump.
            detail = ""
            err = msg.get("error")
            if isinstance(err, dict):
                detail = err.get("error") or err.get("message") or json.dumps(err)
            elif isinstance(err, str):
                detail = err
            if not detail:
                for att in msg.get("attachments", []):
                    if isinstance(att.get("error"), dict):
                        detail = att["error"].get("error") or att["error"].get("message", "")
                    if detail:
                        break
            if not detail:
                detail = json.dumps(msg)[:600]
            out["error"] = (f"Genie did not complete (status={msg.get('status')}): "
                            f"{detail}")
            return out

        # Extract text answer + SQL, and fetch the query result if present
        for att in msg.get("attachments", []):
            if att.get("text"):
                out["answer"] = att["text"].get("content", "")
            if att.get("query"):
                q = att["query"]
                out["sql"] = q.get("query", "")
                att_id = att.get("attachment_id")
                out["dataframe"] = _genie_query_result(base, cid, mid, att_id, headers)
        return out
    except Exception as e:
        out["error"] = f"Genie request failed: {e}"
        return out


def _genie_query_result(base, cid, mid, att_id, headers) -> Optional[pd.DataFrame]:
    """Fetch a Genie attachment's SQL result and build a DataFrame."""
    urls = []
    if att_id:
        urls.append(f"{base}/conversations/{cid}/messages/{mid}/attachments/{att_id}/query-result")
    urls.append(f"{base}/conversations/{cid}/messages/{mid}/query-result")
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code != 200:
                continue
            sr = r.json().get("statement_response", {})
            cols = [c["name"] for c in sr.get("manifest", {}).get("schema", {}).get("columns", [])]
            rows = sr.get("result", {}).get("data_array")
            if cols and rows is not None:
                return pd.DataFrame(rows, columns=cols)
        except Exception:
            continue
    return None


# =============================================================================
# Page: Member Lookup (original NBA scoring)
# =============================================================================

def page_member_lookup():
    st.title("\U0001f3af Next Best Action Console")
    st.caption("Healthcare Payer NBA Engine \u2014 Powered by Databricks Lakebase + Model Serving")

    members = get_all_members()
    if not members:
        st.error("Cannot connect to Lakebase.")
        return

    selected_member = st.selectbox("Select Member ID", members)

    if not selected_member:
        st.info("Select a member to see their Next Best Action.")
        return

    # Fetch member features
    with st.spinner("Fetching member profile from Lakebase..."):
        member = get_member_features(selected_member)

    if not member:
        st.error(f"Member {selected_member} not found.")
        return

    # Member profile card
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Age", member.get("age", "N/A"))
    col2.metric("Churn Risk", f"{float(member.get('churn_risk_score', 0)):.2f}")
    col3.metric("Engagement", f"{float(member.get('engagement_score', 0)):.1f}/10")
    col4.metric("Claims (12m)", member.get("total_claims_12m", 0))

    st.divider()

    # Score actions
    with st.spinner("Scoring actions via model endpoint (first call may take ~60s if waking from cold start)..."):
        actions = get_action_catalog()
        scores = score_actions(member, actions)
        ranked = apply_orchestration(member, actions, scores)

    # Display top recommendation
    if ranked:
        top = ranked[0]
        st.success(f"**\U0001f947 Recommended Action: {top['action_name']}**")

        rec_col1, rec_col2, rec_col3 = st.columns(3)
        rec_col1.markdown(f"**Channel:** {top['channel']}")
        rec_col2.markdown(f"**Category:** {top['category']}")
        rec_col3.markdown(f"**Score:** {top['score']:.4f}")

        st.caption(top["description"])
        st.divider()

    # Full ranked table
    st.subheader("All Ranked Actions")
    df = pd.DataFrame(ranked)
    st.dataframe(
        df[["rank", "action_name", "score", "channel", "category", "team", "compliance"]],
        use_container_width=True,
        hide_index=True,
    )

    # --- Assist: turn the recommendation into a decision ---
    if ranked:
        st.divider()
        st.subheader("\U0001f9e0 Assist")
        top = ranked[0]
        top_action = next((a for a in actions if a["action_id"] == top["action_id"]), {})
        tab_why, tab_draft, tab_whatif, tab_act = st.tabs(
            ["Why this action", "Draft outreach", "What-if", "Approve & act"])

        with tab_why:
            score = float(top["score"])
            band = priority_band(score)
            st.markdown(f"#### Why `{selected_member}` → {top['action_name']} ({band})")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("NBA priority score", f"{score:.2f}")
            m2.metric("Band", band)
            m3.metric("Category", top["category"] or "—")
            m4.metric("Compliance", "Yes" if top["compliance"] else "No")

            if score >= PRIORITY_THRESHOLD:
                st.markdown(f"Priority score **{score:.2f}** exceeds the "
                            f"**{PRIORITY_THRESHOLD:.2f}** action threshold — recommend now.")
            else:
                st.markdown(f"Priority score **{score:.2f}** is below the "
                            f"{PRIORITY_THRESHOLD:.2f} high-priority threshold; recommended "
                            f"as the best available fit for this member.")

            st.markdown("**Reasoning:**")
            for i, r in enumerate(explain_recommendation(member, top_action), 1):
                st.markdown(f"{i}. {r}")

            traj = project_trajectory(member, top_action, score)
            st.markdown("**Predicted trajectory:**")
            st.markdown(f"- Without intervention: {traj['label']} likely to drift to "
                        f"~**{traj['without']:.2f}** (now {traj['current']:.2f})")
            st.markdown(f"- With {top['action_name']}: expected reduction "
                        f"~**{traj['lift_pct']}%** → **{traj['with_new']:.2f}**")
            st.caption("Trajectory is a heuristic projection for illustration, "
                       "not a clinical prediction.")

        with tab_draft:
            default_ch = top["channel"] if top["channel"] in CHANNELS else CHANNELS[0]
            channel = st.selectbox("Channel", CHANNELS,
                                   index=CHANNELS.index(default_ch), key="draft_channel")
            if not LLM_ENDPOINT_NAME:
                st.info("Drafting is disabled — set `LLM_ENDPOINT_NAME` to enable.")
            elif st.button("✍️ Draft outreach message", key="draft_btn"):
                with st.spinner("Drafting via the model endpoint..."):
                    msg = draft_outreach(member, top_action, channel)
                st.session_state["draft_msg"] = msg or ""
                if not msg:
                    st.warning("Could not generate a draft (LLM endpoint unavailable).")
            if st.session_state.get("draft_msg"):
                st.text_area("Draft (review & edit before sending)",
                             st.session_state["draft_msg"], height=170)
                st.caption("Compliance: review before sending. No PHI is included in the prompt.")

        with tab_whatif:
            st.caption("Adjust member signals and re-score to see how the recommendation changes.")
            c1, c2, c3 = st.columns(3)
            wf_churn = c1.slider("Churn risk", 0.0, 1.0, _f(member, "churn_risk_score"), 0.05, key="wf_churn")
            wf_eng = c2.slider("Engagement", 0.0, 10.0, _f(member, "engagement_score"), 0.5, key="wf_eng")
            wf_clin = c3.slider("Clinical risk", 0.0, 1.0, _f(member, "clinical_risk_score"), 0.05, key="wf_clin")
            wf_gap = st.checkbox("Has preventive gap", value=_f(member, "has_preventive_gap") >= 1, key="wf_gap")
            if st.button("\U0001f501 Re-score with these values", key="wf_btn"):
                m2 = dict(member)
                m2.update({
                    "churn_risk_score": wf_churn, "engagement_score": wf_eng,
                    "clinical_risk_score": wf_clin, "has_preventive_gap": 1 if wf_gap else 0,
                    "is_churn_risk": 1 if wf_churn > 0.5 else 0,
                    "is_care_mgmt_candidate": 1 if wf_clin > 0.7 else 0,
                })
                with st.spinner("Re-scoring..."):
                    r2 = apply_orchestration(m2, actions, score_actions(m2, actions))
                new_top = r2[0]
                if new_top["action_id"] != top["action_id"]:
                    st.success(f"New top action: **{new_top['action_name']}** (was {top['action_name']})")
                else:
                    st.info(f"Top action unchanged: **{new_top['action_name']}**")
                st.dataframe(
                    pd.DataFrame(r2)[["rank", "action_name", "score", "channel", "category"]],
                    use_container_width=True, hide_index=True)

        with tab_act:
            st.caption("Approve or adjust the recommendation, then commit. The decision "
                       "is written to the app-writes branch and appears below (closed loop).")
            act_name = st.selectbox("Action to act on", [r["action_name"] for r in ranked],
                                    index=0, key="act_action")
            act_rank = next(r for r in ranked if r["action_name"] == act_name)
            ac1, ac2 = st.columns(2)
            ch_default = act_rank["channel"] if act_rank["channel"] in CHANNELS else CHANNELS[0]
            act_channel = ac1.selectbox("Channel", CHANNELS,
                                        index=CHANNELS.index(ch_default), key="act_channel")
            act_disp = ac2.selectbox("Disposition",
                                     ["Outreach scheduled", "Attempted", "Declined by member", "Deferred"],
                                     key="act_disp")
            act_note = st.text_input("Note (optional)", key="act_note")
            act_approver = st.text_input("Approver", value=get_app_user() or "care_coordinator",
                                         key="act_approver")
            bc1, bc2 = st.columns([3, 1])
            if bc1.button("✅ Approve & commit", type="primary", key="act_commit"):
                if record_decision(selected_member, act_rank["action_id"], act_name,
                                   act_channel, act_rank["score"], "Approved",
                                   act_disp, act_note, act_approver):
                    st.success(f"Decision committed: {selected_member} → {act_name} via {act_channel}.")
                else:
                    st.error("Could not write the decision to the app-writes branch.")
            if bc2.button("🗑️ Dismiss", key="act_dismiss"):
                if record_decision(selected_member, act_rank["action_id"], act_name,
                                   act_channel, act_rank["score"], "Dismissed",
                                   "Dismissed", act_note, act_approver):
                    st.info("Recommendation dismissed and logged.")

            hist = get_decisions(selected_member)
            if hist:
                st.markdown("**Decisions for this member:**")
                hcols = ["created_at", "action_name", "channel", "status",
                         "disposition", "outcome", "approver"]
                st.dataframe(pd.DataFrame(hist)[hcols],
                             use_container_width=True, hide_index=True)

    # Member detail expander
    with st.expander("Member Feature Details"):
        st.json(member)


# =============================================================================
# Page: Manage Actions (CRUD)
# =============================================================================

def page_manage_actions():
    st.title("\u2699\ufe0f Manage Action Catalog")
    st.caption("Add, edit, or remove actions \u2022 Changes are staged here and sync to production after reconciliation")

    st.info("\U0001f6c8 This page shows the **staging** branch (app-writes). "
            "Changes made here will NOT affect Member Lookup scoring until reconciliation runs.")

    # Current actions from app-writes (staged)
    actions = get_action_catalog_staged()
    if not actions:
        st.warning("No actions found or cannot connect to app-writes branch.")
        return

    actions_df = pd.DataFrame(actions)
    display_cols = ['action_id', 'action_name', 'action_category', 'team_owner',
                    'value_score', 'compliance_flag', 'strategic_priority', 'min_spacing_days']
    st.dataframe(actions_df[[c for c in display_cols if c in actions_df.columns]],
                 use_container_width=True, hide_index=True)
    st.metric("Total Actions", len(actions_df))

    st.divider()

    # --- Add New Action ---
    with st.expander("\u2795 Add New Action", expanded=False):
        with st.form("add_action_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                action_id = st.text_input("Action ID", placeholder="ACT013")
                action_name = st.text_input("Action Name", placeholder="Medicare Annual Wellness Visit")
                category = st.selectbox("Category", CATEGORIES)
                team = st.selectbox("Team Owner", TEAMS)
            with col2:
                value_score = st.slider("Value Score", 0, 100, 75)
                priority = st.selectbox("Strategic Priority", [1, 2, 3, 4, 5])
                compliance = st.checkbox("Compliance/Regulatory")
                spacing = st.number_input("Min Spacing Days", 7, 365, 30)
            description = st.text_area("Description", placeholder="Brief description of the action")
            channels = st.multiselect("Eligible Channels", CHANNELS, default=["Digital", "Call center"])

            submitted = st.form_submit_button("\u2795 Add Action", type="primary")
            if submitted:
                if not action_id or not action_name:
                    st.error("Action ID and Name are required.")
                else:
                    channels_json = json.dumps(channels)  # JSON array: ["Digital", "Call center"]
                    action_data = {
                        "action_id": action_id,
                        "action_name": action_name,
                        "action_category": category,
                        "team_owner": team,
                        "description": description,
                        "value_score": value_score,
                        "compliance_flag": compliance,
                        "strategic_priority": priority,
                        "min_spacing_days": spacing,
                        "eligible_channels": channels_json,
                    }
                    if add_action(action_data):
                        st.success(f"\u2705 Action {action_id} added! Refresh to see it.")
                        st.cache_resource.clear()
                    else:
                        st.error("Failed to add action.")

    # --- Edit Existing Action ---
    with st.expander("\u270f\ufe0f Edit Action", expanded=False):
        action_options = {f"{r['action_id']} - {r['action_name']}": r for r in actions}
        selected = st.selectbox("Select Action to Edit", list(action_options.keys()), key="edit_select")
        action = action_options[selected]

        with st.form("edit_action_form"):
            col1, col2 = st.columns(2)
            with col1:
                new_name = st.text_input("Action Name", value=action['action_name'])
                cat_idx = CATEGORIES.index(action['action_category']) if action.get('action_category') in CATEGORIES else 0
                new_category = st.selectbox("Category", CATEGORIES, index=cat_idx)
                team_idx = TEAMS.index(action['team_owner']) if action.get('team_owner') in TEAMS else 0
                new_team = st.selectbox("Team Owner", TEAMS, index=team_idx)
            with col2:
                new_value = st.slider("Value Score", 0, 100, int(action.get('value_score', 75)))
                new_priority = st.selectbox("Strategic Priority", [1, 2, 3, 4, 5],
                                            index=int(action.get('strategic_priority', 3)) - 1)
                new_compliance = st.checkbox("Compliance", value=bool(action.get('compliance_flag')))
                new_spacing = st.number_input("Min Spacing Days", 7, 365, int(action.get('min_spacing_days', 30)))

            col_save, col_delete = st.columns([3, 1])
            with col_save:
                save_btn = st.form_submit_button("\U0001f4be Save Changes", type="primary")
            with col_delete:
                delete_btn = st.form_submit_button("\U0001f5d1\ufe0f Delete Action")

            if save_btn:
                updates = {
                    "action_name": new_name,
                    "action_category": new_category,
                    "team_owner": new_team,
                    "value_score": new_value,
                    "strategic_priority": new_priority,
                    "compliance_flag": new_compliance,
                    "min_spacing_days": new_spacing,
                }
                if update_action(action['action_id'], updates):
                    st.success(f"\u2705 Action {action['action_id']} updated!")
                    st.cache_resource.clear()
                else:
                    st.error("Failed to update.")

            if delete_btn:
                if delete_action(action['action_id']):
                    st.success(f"\U0001f5d1\ufe0f Action {action['action_id']} deleted.")
                    st.cache_resource.clear()
                else:
                    st.error("Failed to delete.")


# =============================================================================
# Page: Change Log
# =============================================================================

def page_change_log():
    # NOTE (CDF design): This page reads the legacy Postgres audit table
    # `_action_catalog_changes`, populated by the app-writes trigger. It is
    # KEPT FOR TESTING / parallel-run validation only and is NOT required by
    # the new design — reconciliation is driven by Lakebase Change Data Feed
    # (nba_reconcile reads <cdf_catalog>.<cdf_schema>.lb_action_catalog_history
    # by LSN watermark), not by this table. Safe to retire the trigger +
    # _action_catalog_changes after cutover, or repoint this page at the CDF
    # history table.
    st.title("\U0001f4dc Change Log")
    st.caption("Legacy audit view (Postgres trigger on app-writes). Kept for "
               "testing only — reconciliation is now driven by Lakebase CDF, "
               "not this table.")

    changes = get_change_log()
    if changes.empty:
        st.info("No changes recorded yet. Edits made on the Manage Actions page will appear here.")
    else:
        st.dataframe(changes, use_container_width=True, hide_index=True)
        st.metric("Total Changes", len(changes))


# =============================================================================
# Page: Ask NBA (Genie agent)
# =============================================================================

ASK_NBA_SUGGESTIONS = [
    "How many members have an open care gap by measure?",
    "Open care gaps by market",
    "What is the overall gap closure rate?",
    "NBA acceptance rate by channel",
    "Average value score by owning team",
    "Total predicted cost of the churn-risk cohort",
]


def _run_ask_nba(question: str):
    """Send a question to Genie and append the exchange to session history."""
    with st.spinner("Asking Genie (first question may wake the SQL warehouse ~30s)..."):
        res = genie_ask(question, st.session_state.get("genie_conversation_id"))
    if res.get("conversation_id"):
        st.session_state["genie_conversation_id"] = res["conversation_id"]
    st.session_state.setdefault("genie_history", []).append({"q": question, "res": res})


def page_ask_nba():
    st.title("\U0001f4ac Ask NBA")
    st.caption("Natural-language analytics over the book of business — powered by "
               "Databricks Genie (Unity Catalog + SQL warehouse).")

    if not GENIE_SPACE_ID:
        st.warning("Genie is not configured. Set `GENIE_SPACE_ID` (bundle variable "
                   "`genie_space_id`) to enable this page.")
        return

    # Suggested questions
    st.markdown("**Try a question:**")
    cols = st.columns(3)
    for i, s in enumerate(ASK_NBA_SUGGESTIONS):
        if cols[i % 3].button(s, key=f"sugg_{i}", use_container_width=True):
            _run_ask_nba(s)

    # New-conversation control
    if st.session_state.get("genie_history"):
        if st.button("\U0001f504 New conversation"):
            st.session_state["genie_history"] = []
            st.session_state["genie_conversation_id"] = None

    # Render conversation history
    for turn in st.session_state.get("genie_history", []):
        with st.chat_message("user"):
            st.markdown(turn["q"])
        with st.chat_message("assistant"):
            res = turn["res"]
            if res.get("error"):
                st.error(res["error"])
            else:
                if res.get("answer"):
                    st.markdown(res["answer"])
                if res.get("sql"):
                    with st.expander("View generated SQL"):
                        st.code(res["sql"], language="sql")
                df = res.get("dataframe")
                if df is not None and not df.empty:
                    st.dataframe(df, use_container_width=True, hide_index=True)

    # Free-form chat input
    prompt = st.chat_input("Ask a question about members, care gaps, actions...")
    if prompt:
        _run_ask_nba(prompt)
        st.rerun()


# =============================================================================
# Page: Decisions (Approve & Act audit + outcome capture)
# =============================================================================

def page_decisions():
    st.title("✅ Decisions")
    st.caption("Closed-loop audit of committed NBA decisions (app-writes branch). "
               "Record outcomes as they land — reflected on the next read.")

    rows = get_decisions()
    if not rows:
        st.info("No decisions yet. Approve one on **Member Lookup → \U0001f9e0 Assist → Approve & act**.")
        return

    df = pd.DataFrame(rows)
    c1, c2, c3 = st.columns(3)
    c1.metric("Total decisions", len(df))
    c2.metric("Approved", int((df["status"] == "Approved").sum()))
    c3.metric("Outcomes recorded", int(df["outcome"].notna().sum()))

    st.dataframe(
        df[["created_at", "member_id", "action_name", "channel", "status",
            "disposition", "outcome", "approver"]],
        use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Record an outcome")
    opts = {f"#{r['decision_id']} · {r['member_id']} · {r['action_name']} ({r['status']})":
            r["decision_id"] for r in rows}
    sel = st.selectbox("Decision", list(opts.keys()))
    outcome = st.selectbox("Outcome",
                           ["Gap Closed", "Enrolled", "Retained", "No Response", "None"])
    if st.button("\U0001f4be Save outcome"):
        if update_decision_outcome(opts[sel], outcome):
            st.success("Outcome saved — reflected on next read.")
        else:
            st.error("Could not update the outcome.")


# =============================================================================
# Main App Navigation
# =============================================================================

def main():
    st.set_page_config(page_title="NBA Console", page_icon="\U0001f3af", layout="wide")

    # Sidebar navigation
    with st.sidebar:
        st.header("NBA Console")
        page = st.radio(
            "Navigate",
            ["\U0001f3af Member Lookup", "\u2705 Decisions", "\u2699\ufe0f Manage Actions",
             "\U0001f4dc Change Log", "\U0001f4ac Ask NBA"],
            label_visibility="collapsed",
        )
        st.divider()
        st.markdown(f"**Scoring:** `{LAKEBASE_BRANCH_PRODUCTION}`")
        st.markdown(f"**Staging:** `{LAKEBASE_BRANCH_APP_WRITES}`")
        if LAKEBASE_PROJECT:
            st.markdown(f"**Project:** `{LAKEBASE_PROJECT}`")
        st.markdown(f"**Schema:** `{LAKEBASE_SCHEMA}`")
        st.markdown(f"**Model:** `{MODEL_ENDPOINT_NAME}`")
        if GENIE_SPACE_ID:
            st.markdown(f"**Genie:** `{GENIE_SPACE_ID}`")

    # Route to selected page
    if page == "\U0001f3af Member Lookup":
        page_member_lookup()
    elif page == "\u2705 Decisions":
        page_decisions()
    elif page == "\u2699\ufe0f Manage Actions":
        page_manage_actions()
    elif page == "\U0001f4dc Change Log":
        page_change_log()
    elif page == "\U0001f4ac Ask NBA":
        page_ask_nba()


if __name__ == "__main__":
    main()
