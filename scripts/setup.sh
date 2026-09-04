#!/usr/bin/env bash
# =============================================================================
# One-command setup / recovery for the NBA Prototype demo.
#
#   ./scripts/setup.sh <target>          # full fresh install (default target: dev)
#   ./scripts/setup.sh azure
#
# Runs the whole install sequence so you don't have to remember the steps:
#   deploy(jobs) -> seed -> train -> bootstrap -> deploy(apps) -> bootstrap(grant)
#   -> grant React app SP -> start BOTH apps (Streamlit + React) -> verify CDF
#
# Deploys and starts BOTH Databricks Apps: the Streamlit console (app_name) and
# the React + FastAPI console (app_name_react). They share the same Lakebase +
# endpoint resources and env-var contract.
#
# Idempotent: safe to re-run if something was deleted. Everything is resolved
# from databricks.yml, so this works in ANY workspace after you set the target's
# host + variables. The only thing it can't do is enable CDF (UI-only) — it
# prints exact instructions and pauses.
# =============================================================================
source "$(dirname "$0")/_lib.sh"

TARGET="${1:-fevm}"
require_cli
load_bundle_config "$TARGET"

echo
printf "${c_bold}NBA Prototype — automated setup${c_reset}\n"
cat <<EOF
  Target:     $TARGET
  Workspace:  $WORKSPACE_HOST
  Catalog:    $UC_CATALOG   Lakebase project: $LAKEBASE_PROJECT
  Apps:       $APP_NAME (Streamlit)${APP_NAME_REACT:+, $APP_NAME_REACT (React)}

This runs the full install. It takes ~15-20 min (model training is the long part).
EOF

step "1/7  Deploy bundle (jobs) — app resource may error on first pass (expected)"
databricks bundle deploy -t "$TARGET" || warn "Deploy reported an issue (often the app resource on first pass — expected). Continuing."

run_job nba_seed_data   "$TARGET"   # 2/7 seed UC source tables
run_job nba_train_model "$TARGET"   # 3/7 train + deploy scoring endpoint
run_job nba_bootstrap   "$TARGET"   # 4/7 reset+seed Lakebase, fork app-writes (SP grant skipped: app not yet deployed)

step "5/8  Deploy again — now app-writes branch + endpoint exist, so BOTH apps are created"
databricks bundle deploy -t "$TARGET"

run_job nba_bootstrap   "$TARGET"   # 6/8 re-run so the Streamlit app SP is auto-resolved + granted

# Grant the Streamlit app SP everything the "Ask NBA" (Genie) page needs. No-op
# unless genie_space_id is set for this target, so non-Genie targets are unaffected.
grant_genie_access "$APP_NAME"

# 7/8  Grant the React app's SP Lakebase access (read production + read/write
# app-writes), mirroring the bootstrap grant for the Streamlit SP. The React app
# was deployed in step 5, so its SP now resolves. Non-destructive; safe to
# re-run. Then give it the same Genie access. Best-effort so a React-only hiccup
# never blocks the (already-installed) core system.
if [ -n "${APP_NAME_REACT:-}" ]; then
  step "7/8  Grant Lakebase + Genie access to the React app SP"
  run_job nba_grant_react_sp "$TARGET" || warn "Could not grant the React app SP; run 'databricks bundle run nba_grant_react_sp -t $TARGET' after the app finishes deploying."
  grant_genie_access "$APP_NAME_REACT"
else
  warn "app_name_react not set for this target — skipping the React app grants."
fi

step "8/8  Start both apps"
databricks bundle run nba_console -t "$TARGET" \
  || warn "Could not auto-start the Streamlit app; start it from the Apps UI if needed."
if [ -n "${APP_NAME_REACT:-}" ]; then
  databricks bundle run nba_console_react -t "$TARGET" \
    || warn "Could not auto-start the React app; start it from the Apps UI if needed."
fi

ok "Install complete."

# CDF is enabled automatically by nba_bootstrap (steps 4/6) via the Lakebase CDF
# API — no manual step. Verify it's streaming; if the API wasn't available in
# this workspace, bootstrap printed the one-time manual UI step in its output.
step "Verifying CDF is enabled on the app-writes branch"
if verify_cdf_streaming; then
  ok "CDF is streaming — the full loop is ready."
else
  warn "Could not confirm CDF is streaming from here. Check the nba_bootstrap"
  warn "job output: it enables CDF via API, or prints the manual UI step if the"
  warn "API is unavailable in this workspace."
  print_cdf_enable_banner
fi

# If this was a re-install after a destroy, the app host may not resolve locally
# yet due to the databricksapps zone's 24h negative-cache TTL. Flag it (the app
# itself is fine) so the user isn't surprised by a browser "site can't be reached".
app_url() { databricks apps get "$1" -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("url",""))' 2>/dev/null; }
APP_URL="$(app_url "$APP_NAME")"
APP_URL_REACT=""; [ -n "${APP_NAME_REACT:-}" ] && APP_URL_REACT="$(app_url "$APP_NAME_REACT")"
[ -n "$APP_URL" ]       && warn_if_app_dns_stale "$APP_URL"
[ -n "$APP_URL_REACT" ] && warn_if_app_dns_stale "$APP_URL_REACT"

printf "${c_bold}NEXT:${c_reset}\n"
cat <<EOF
  • Run the reconcile demo:      ./scripts/demo.sh $TARGET
  • Streamlit app URL: ${APP_URL:-$WORKSPACE_HOST (Apps -> $APP_NAME)}
$( [ -n "${APP_NAME_REACT:-}" ] && echo "  • React app URL:     ${APP_URL_REACT:-$WORKSPACE_HOST (Apps -> $APP_NAME_REACT)}" )
EOF
