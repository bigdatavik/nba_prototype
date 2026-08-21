#!/usr/bin/env bash
# =============================================================================
# Shared helpers for the NBA demo scripts. Source this from setup.sh / demo.sh.
#
# NOTHING is hardcoded to a workspace. Every value is resolved from the bundle
# (databricks bundle summary -t <target> -o json), so these scripts work in ANY
# workspace once you've set the target's host + variables in databricks.yml.
#
# Usage in a script:
#   TARGET="${1:-dev}"
#   source "$(dirname "$0")/_lib.sh"
#   load_bundle_config "$TARGET"
#   # now $UC_CATALOG, $LAKEBASE_PROJECT, $APP_NAME, ... are set
# =============================================================================
set -euo pipefail

# Repo root = parent of the scripts/ dir this file lives in.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

c_reset='\033[0m'; c_bold='\033[1m'; c_green='\033[32m'
c_yellow='\033[33m'; c_red='\033[31m'; c_blue='\033[36m'
say()   { printf "${c_blue}%s${c_reset}\n" "$*"; }
ok()    { printf "${c_green}✓ %s${c_reset}\n" "$*"; }
warn()  { printf "${c_yellow}⚠ %s${c_reset}\n" "$*"; }
err()   { printf "${c_red}✗ %s${c_reset}\n" "$*" >&2; }
step()  { printf "\n${c_bold}==> %s${c_reset}\n" "$*"; }

die() { err "$*"; exit 1; }

require_cli() {
  command -v databricks >/dev/null 2>&1 || die "Databricks CLI not found. Install v0.239.0+ (https://docs.databricks.com/dev-tools/cli/)."
  command -v python3   >/dev/null 2>&1 || die "python3 not found."
}

# Resolve bundle config for a target into shell vars. No hardcoded names.
load_bundle_config() {
  local target="$1"
  cd "$REPO_ROOT"
  [ -f databricks.yml ] || die "databricks.yml not found. Copy databricks.yml.template to databricks.yml and set your host + variables first."

  say "Resolving bundle config for target '$target'…"
  local json
  json="$(databricks bundle summary -t "$target" -o json 2>/dev/null)" \
    || die "Could not read bundle summary for target '$target'. Is the target defined in databricks.yml and are you authenticated?"

  # Pull every variable + host via python (portable; no jq dependency).
  eval "$(printf '%s' "$json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
v = d.get("variables", {})
def g(k, default=""):
    val = v.get(k, {})
    out = val.get("value") if isinstance(val, dict) else val
    return out if out not in (None, "") else default
pairs = {
  "UC_CATALOG":        g("uc_catalog"),
  "UC_SCHEMA":         g("uc_schema"),
  "LAKEBASE_PROJECT":  g("lakebase_project"),
  "LAKEBASE_SCHEMA":   g("lakebase_schema"),
  "LAKEBASE_DATABASE": g("lakebase_database", "databricks_postgres"),
  "LAKEBASE_DATABASE_ID": g("lakebase_database_id", "databricks-postgres"),
  "PROD_BRANCH":       g("lakebase_branch", "production"),
  "APP_WRITES_BRANCH": g("app_writes_branch", "app-writes"),
  "CDF_CATALOG":       g("cdf_catalog"),
  "CDF_SCHEMA":        g("cdf_schema"),
  "APP_NAME":          g("app_name"),
  "MODEL_ENDPOINT":    g("model_endpoint_name", "nba-scoring-endpoint"),
  "WORKSPACE_HOST":    d.get("workspace", {}).get("host", ""),
}
for k, val in pairs.items():
    print(f"export {k}=%s" % json.dumps(str(val)))
')"

  CDF_HISTORY_TABLE="${CDF_CATALOG}.${CDF_SCHEMA}.lb_action_catalog_history"
  export CDF_HISTORY_TABLE
  [ -n "${UC_CATALOG:-}" ]       || die "uc_catalog not resolved from bundle."
  [ -n "${LAKEBASE_PROJECT:-}" ] || die "lakebase_project not resolved from bundle."
  ok "Config resolved: host=${WORKSPACE_HOST} catalog=${UC_CATALOG} project=${LAKEBASE_PROJECT} app=${APP_NAME}"
}

# Run a bundle job and stream status (blocks until done).
run_job() { local job="$1"; local target="$2"; shift 2
  step "Running job: $job (target $target)"
  databricks bundle run "$job" -t "$target" "$@"
}

# Print the exact CDF-enable UI steps for this env. Only needed as a FALLBACK:
# nba_bootstrap enables CDF automatically via the Lakebase CDF API. Use this if
# the API is unavailable in a given workspace.
print_cdf_enable_banner() {
  cat <<EOF

$(printf "${c_yellow}${c_bold}")============================================================
  FALLBACK — ENABLE CDF MANUALLY (only if the API is unavailable)
============================================================$(printf "${c_reset}")
nba_bootstrap normally enables CDF automatically via the Lakebase CDF API.
If that API is unavailable in this workspace, enable it by hand ONCE on the
app-writes branch:

  1. Open: ${WORKSPACE_HOST}/compute/lakebase
  2. Project '${LAKEBASE_PROJECT}' -> branch '${APP_WRITES_BRANCH}' -> 'Lakebase CDF' tab
  3. Click Start. Under Tables add:
        source:      ${LAKEBASE_SCHEMA}.action_catalog
        destination: ${CDF_CATALOG}.${CDF_SCHEMA}   (schema; table auto-named lb_action_catalog_history)
  4. Confirm status shows 'Enabled' with a 'Committed LSN'.

Verify later with:
  SELECT _pg_change_type, count(*) FROM ${CDF_HISTORY_TABLE} GROUP BY 1;
============================================================

EOF
}

# Return 0 if Lakebase CDF for action_catalog on the app-writes branch is
# STREAMING, else 1. Uses the CDF status API via the CLI (no hardcoded host).
verify_cdf_streaming() {
  local parent="projects/${LAKEBASE_PROJECT}/branches/${APP_WRITES_BRANCH}/databases/${LAKEBASE_DATABASE_ID}/cdf-configs/${LAKEBASE_SCHEMA}"
  local out
  out="$(databricks api get "/api/2.0/postgres/${parent}/cdf-statuses" 2>/dev/null)" || return 1
  printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for st in d.get("cdf_statuses", []):
    if st.get("postgres_table") == "action_catalog" and st.get("state") == "CDF_STATE_STREAMING":
        print("  committed_lsn=%s uc_table=%s" % (st.get("committed_lsn"), st.get("uc_table")))
        sys.exit(0)
sys.exit(1)
'
}

# Echo the id of a usable SQL warehouse (prefer a RUNNING one), or empty string.
find_warehouse() {
  databricks warehouses list -o json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ws = d if isinstance(d, list) else d.get("warehouses", [])
run = [w for w in ws if str(w.get("state")) == "RUNNING"]
pick = (run or ws)
print(pick[0]["id"] if pick else "")
'
}

# Run a SQL statement on a warehouse; prints the final state. Returns non-zero
# if the statement did not succeed. Usage: run_sql <warehouse_id> "<sql>"
run_sql() {
  local wid="$1"; local sql="$2"
  local body
  body="$(python3 -c 'import json,sys; print(json.dumps({"warehouse_id":sys.argv[1],"statement":sys.argv[2],"wait_timeout":"30s"}))' "$wid" "$sql")"
  databricks api post /api/2.0/sql/statements --json "$body" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR"); sys.exit(1)
state = d.get("status", {}).get("state", "UNKNOWN")
print(state)
sys.exit(0 if state == "SUCCEEDED" else 1)
'
}

# Check whether an app URL resolves in DNS from this machine, and print guidance
# if it doesn't. This matters right after a destroy+recreate: the databricksapps
# zone has a 24h NEGATIVE-cache TTL, so a resolver that cached "no such host"
# while the app was destroyed can keep failing for hours even though the app is
# healthy. A brand-new deploy (never destroyed) won't hit this. Usage:
#   warn_if_app_dns_stale "https://<app-host>"
warn_if_app_dns_stale() {
  local url="$1"
  local host="${url#https://}"; host="${host%%/*}"
  [ -z "$host" ] && return 0
  # Resolve via the OS resolver; if it fails but a public resolver succeeds, the
  # local cache is stale.
  if getent hosts "$host" >/dev/null 2>&1 || nslookup "$host" >/dev/null 2>&1; then
    return 0   # resolves fine locally
  fi
  if nslookup "$host" 8.8.8.8 >/dev/null 2>&1; then
    warn "The app host does not resolve on this machine yet, but it DOES via a"
    warn "public resolver — a stale DNS negative-cache (from a prior destroy)."
    warn "The app itself is healthy. Clear the cache to reach it:"
    echo  "    • Chrome:  open chrome://net-internals/#dns  -> Clear host cache"
    echo  "    • macOS:   sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder"
    echo  "    • or set your Wi-Fi DNS to 8.8.8.8, then reload."
  fi
}
