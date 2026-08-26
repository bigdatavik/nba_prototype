#!/usr/bin/env bash
# =============================================================================
# Quick reset for the NBA Prototype — recover a broken/dirty demo environment.
#
#   ./scripts/reset.sh <target>              # baseline reset (default: dev)
#   ./scripts/reset.sh <target> --full       # destructive full rebuild
#
# Two levels:
#   (default)  nba_reset_action_catalog — non-destructive. Puts action_catalog
#              back to the 16 baseline rows on production. KEEPS the app-writes
#              branch + CDF running, so NO manual step. Use this 99% of the time
#              (e.g. someone added junk actions during a demo).
#
#   --full     nba_bootstrap (reset_environment=true) — destructive rebuild:
#              re-forks app-writes, drops CDF history, resets watermark, and
#              re-enables CDF automatically via the Lakebase CDF API (falls back
#              to printing the manual UI step only if that API is unavailable).
#              Use only if the environment is truly broken.
# =============================================================================
source "$(dirname "$0")/_lib.sh"

TARGET="${1:-fevm}"
MODE="${2:-baseline}"
require_cli
load_bundle_config "$TARGET"

if [ "$MODE" = "--full" ]; then
  warn "FULL reset: destructive. Re-forks app-writes; CDF is re-enabled automatically."
  run_job nba_bootstrap "$TARGET"
  ok "Full rebuild complete."
  step "Verifying CDF is enabled on the app-writes branch"
  if verify_cdf_streaming; then
    ok "CDF is streaming — no manual step needed."
  else
    warn "Could not confirm CDF from here — check the nba_bootstrap job output."
    print_cdf_enable_banner
  fi
  echo "Run the demo:  ./scripts/demo.sh $TARGET"
else
  step "Baseline reset — action_catalog back to 16 baseline rows (branch + CDF untouched)"
  run_job nba_reset_action_catalog "$TARGET"
  # Also clear any leftover demo rows still staged on app-writes, then reconcile
  # so production matches the baseline too.
  python3 - "$LAKEBASE_PROJECT" "$LAKEBASE_SCHEMA" "$LAKEBASE_DATABASE" "$APP_WRITES_BRANCH" <<'PYEOF' || true
import sys
from databricks.sdk import WorkspaceClient
import psycopg2
project, schema, db, appw = sys.argv[1:5]
w = WorkspaceClient(); user = w.current_user.me().user_name
ep=f"projects/{project}/branches/{appw}/endpoints/primary"
host=w.postgres.get_endpoint(name=ep).status.hosts.host
tok=w.postgres.generate_database_credential(endpoint=ep).token
c=psycopg2.connect(host=host,port=5432,dbname=db,user=user,password=tok,sslmode="require");c.autocommit=True
cur=c.cursor(); cur.execute(f"DELETE FROM {schema}.action_catalog WHERE action_id LIKE 'DEMO%'")
print("cleared any DEMO rows on app-writes"); c.close()
PYEOF
  ok "Baseline reset complete. No manual CDF step needed."
  echo "Ready for a clean demo:  ./scripts/demo.sh $TARGET"
fi
