"""
Configuration — Dual Branch Architecture (fully environment-driven).

Copied verbatim (behavior-preserving) from src/app/app.py. Nothing here is
hardcoded to a specific workspace, Lakebase project, or endpoint host — the same
image runs in dev, prod, or any customer environment by changing config.

PRODUCTION branch: member_features (read-only, synced daily from UC)
APP-WRITES branch: action_catalog (CRUD) + audit trail

Lakebase endpoints are expressed as their resource paths:
    projects/<project>/branches/<branch>/endpoints/primary
and the actual Postgres host is resolved at runtime via the SDK. This makes the
app self-healing across branch re-forks (host changes) with no redeploy.
"""

import os
from typing import Optional

# =============================================================================
# Environment-driven configuration (same env-var names as the Streamlit app)
# =============================================================================

LAKEBASE_DATABASE = os.getenv("LAKEBASE_DATABASE", "databricks_postgres")
LAKEBASE_SCHEMA = os.getenv("LAKEBASE_SCHEMA", "nba_new_lbase")
MODEL_ENDPOINT_NAME = os.getenv("MODEL_ENDPOINT_NAME", "nba-scoring-endpoint")

# Genie Space for the "Ask NBA" page (natural-language analytics over UC). Blank
# disables the page. The space is bound to its own SQL warehouse at creation, so
# the app only needs the space id.
GENIE_SPACE_ID = os.getenv("GENIE_SPACE_ID", "")

# Foundation Model (chat) endpoint used by the per-member "Assist" — drafts the
# outreach message. Blank disables the draft feature (reason codes + what-if
# still work). Any Databricks FM chat endpoint (llm/v1/chat) works.
LLM_ENDPOINT_NAME = os.getenv("LLM_ENDPOINT_NAME", "")

# Lakebase project + branch names → endpoint resource paths.
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
PRODUCTION_HOST_OVERRIDE = os.getenv("LAKEBASE_HOST_PRODUCTION", "")
APP_WRITES_HOST_OVERRIDE = os.getenv("LAKEBASE_HOST_APP_WRITES", "")


# =============================================================================
# Reference values used by the UI (dropdowns + threshold)
# =============================================================================

CATEGORIES = ["STARS", "MRA", "Pharmacy", "PCO", "Home health", "Regulatory"]
TEAMS = ["Clinical innovation", "Stars", "Pharmacy", "PCO", "Home health", "Regulatory", "MRA"]
CHANNELS = ["Digital", "Call center", "Provider", "E/Mail"]
PRIORITY_THRESHOLD = 0.66

# Suggested questions surfaced on the Ask NBA page / Genie launcher.
ASK_NBA_SUGGESTIONS = [
    "How many members have an open care gap by measure?",
    "Open care gaps by market",
    "What is the overall gap closure rate?",
    "NBA acceptance rate by channel",
    "Average value score by owning team",
    "Total predicted cost of the churn-risk cohort",
]


# =============================================================================
# Host resolution helpers
# =============================================================================

def get_databricks_host() -> str:
    """Resolve the workspace host from the SDK config (env DATABRICKS_HOST or the
    app's injected credentials). No workspace is hardcoded."""
    try:
        from databricks.sdk.core import Config
        cfg = Config()
        host = (cfg.host or "").replace("https://", "").replace("http://", "").rstrip("/")
        if host:
            return host
    except Exception:
        pass
    return os.getenv("DATABRICKS_HOST", "").replace("https://", "").replace("http://", "").rstrip("/")


def get_app_user(headers: Optional[dict] = None) -> str:
    """Best-effort end-user email forwarded by Databricks Apps (else blank).

    In the Streamlit app this came from st.context.headers; here the FastAPI
    request headers are passed in (X-Forwarded-Email)."""
    if not headers:
        return ""
    try:
        # Header lookups are case-insensitive on Starlette's Headers, but accept
        # plain dicts too.
        for key in ("X-Forwarded-Email", "x-forwarded-email"):
            val = headers.get(key)
            if val:
                return val
    except Exception:
        pass
    return ""
