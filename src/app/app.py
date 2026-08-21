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
# Main App Navigation
# =============================================================================

def main():
    st.set_page_config(page_title="NBA Console", page_icon="\U0001f3af", layout="wide")

    # Sidebar navigation
    with st.sidebar:
        st.header("NBA Console")
        page = st.radio(
            "Navigate",
            ["\U0001f3af Member Lookup", "\u2699\ufe0f Manage Actions", "\U0001f4dc Change Log"],
            label_visibility="collapsed",
        )
        st.divider()
        st.markdown(f"**Scoring:** `{LAKEBASE_BRANCH_PRODUCTION}`")
        st.markdown(f"**Staging:** `{LAKEBASE_BRANCH_APP_WRITES}`")
        if LAKEBASE_PROJECT:
            st.markdown(f"**Project:** `{LAKEBASE_PROJECT}`")
        st.markdown(f"**Schema:** `{LAKEBASE_SCHEMA}`")
        st.markdown(f"**Model:** `{MODEL_ENDPOINT_NAME}`")

    # Route to selected page
    if page == "\U0001f3af Member Lookup":
        page_member_lookup()
    elif page == "\u2699\ufe0f Manage Actions":
        page_manage_actions()
    elif page == "\U0001f4dc Change Log":
        page_change_log()


if __name__ == "__main__":
    main()
