#!/usr/bin/env bash
# =============================================================================
# One-command setup / recovery for the NBA Prototype demo.
#
#   ./scripts/setup.sh <target>          # full fresh install (default target: dev)
#   ./scripts/setup.sh azure
#
# Runs the whole install sequence so you don't have to remember the steps:
#   deploy(jobs) -> seed -> train -> bootstrap -> deploy(app) -> bootstrap(grant)
#   -> start app -> print the ONE manual step (enable CDF)
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
  Catalog:    $UC_CATALOG   Lakebase project: $LAKEBASE_PROJECT   App: $APP_NAME

This runs the full install. It takes ~15-20 min (model training is the long part).
EOF

step "1/7  Deploy bundle (jobs) — app resource may error on first pass (expected)"
databricks bundle deploy -t "$TARGET" || warn "Deploy reported an issue (often the app resource on first pass — expected). Continuing."

run_job nba_seed_data   "$TARGET"   # 2/7 seed UC source tables
run_job nba_train_model "$TARGET"   # 3/7 train + deploy scoring endpoint
run_job nba_bootstrap   "$TARGET"   # 4/7 reset+seed Lakebase, fork app-writes (SP grant skipped: app not yet deployed)

step "5/7  Deploy again — now app-writes branch + endpoint exist, so the app is created"
databricks bundle deploy -t "$TARGET"

run_job nba_bootstrap   "$TARGET"   # 6/7 re-run so the app SP is auto-resolved + granted

step "7/7  Start the app"
databricks bundle run nba_console -t "$TARGET" \
  || warn "Could not auto-start the app; start it from the Apps UI if needed."

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
APP_URL="$(databricks apps get "$APP_NAME" -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("url",""))' 2>/dev/null)"
[ -n "$APP_URL" ] && warn_if_app_dns_stale "$APP_URL"

printf "${c_bold}NEXT:${c_reset}\n"
cat <<EOF
  • Run the reconcile demo:   ./scripts/demo.sh $TARGET
  • App URL: ${APP_URL:-$WORKSPACE_HOST (Apps -> $APP_NAME)}
EOF
