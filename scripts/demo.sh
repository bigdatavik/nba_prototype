#!/usr/bin/env bash
# =============================================================================
# One-command reconcile DEMO for the NBA Prototype.
#
#   ./scripts/demo.sh <target>        # default target: dev
#   ./scripts/demo.sh azure
#
# Tells the CDF story end-to-end, live:
#   1. adds a sample action on the app-writes branch (like an app user editing)
#   2. waits for Lakebase CDF to capture it into the UC history table
#   3. runs nba_reconcile (CDF -> UC -> production)
#   4. verifies the action is now live on the production branch
#
# Everything is resolved from databricks.yml (works in any workspace).
# Prereq: setup.sh has run AND CDF is enabled on the app-writes branch.
# Cleanup: ./scripts/demo.sh <target> --cleanup   removes the demo action.
# =============================================================================
source "$(dirname "$0")/_lib.sh"

TARGET="${1:-dev}"
MODE="${2:-run}"     # run | --cleanup
require_cli
load_bundle_config "$TARGET"

DEMO_ID="DEMO$(date +%H%M%S)"          # unique per run so re-runs never collide
[ "$MODE" = "--cleanup" ] && DEMO_ID="DEMO_CLEANUP_ALL"

# --- Python helper: do Lakebase/CDF work via the SDK (host resolved at runtime) ---
run_py() {
python3 - "$@" <<'PYEOF'
import sys, json, time
from databricks.sdk import WorkspaceClient
import psycopg2

action = sys.argv[1]
project = sys.argv[2]; schema = sys.argv[3]; db = sys.argv[4]
prod_branch = sys.argv[5]; appw_branch = sys.argv[6]
demo_id = sys.argv[7]

w = WorkspaceClient()
user = w.current_user.me().user_name

def connect(branch):
    ep = f"projects/{project}/branches/{branch}/endpoints/primary"
    host = w.postgres.get_endpoint(name=ep).status.hosts.host
    tok = w.postgres.generate_database_credential(endpoint=ep).token
    c = psycopg2.connect(host=host, port=5432, dbname=db, user=user,
                         password=tok, sslmode="require")
    c.autocommit = True
    return c

if action == "insert":
    c = connect(appw_branch); cur = c.cursor()
    cur.execute(f"""
        INSERT INTO {schema}.action_catalog
          (action_id, action_name, action_category, team_owner, description,
           value_score, compliance_flag, strategic_priority, eligible_channels, min_spacing_days)
        VALUES (%s,'Demo: Wellness Outreach','Engagement','Demo Team',
                'Sample action added by demo.sh',82,true,3,%s,14)
        ON CONFLICT (action_id) DO UPDATE SET value_score=EXCLUDED.value_score
    """, (demo_id, json.dumps(["Digital","Call center"])))
    print(f"INSERTED {demo_id} on {appw_branch}")
    c.close()

elif action == "cleanup":
    for br in (appw_branch, prod_branch):
        c = connect(br); cur = c.cursor()
        cur.execute(f"DELETE FROM {schema}.action_catalog WHERE action_id LIKE 'DEMO%'")
        print(f"cleaned DEMO rows on {br}")
        c.close()

elif action == "show_prod":
    c = connect(prod_branch); cur = c.cursor()
    cur.execute(f"SELECT action_id, action_name, value_score, eligible_channels "
                f"FROM {schema}.action_catalog WHERE action_id=%s", (demo_id,))
    r = cur.fetchone()
    print("PRODUCTION_ROW:" + (json.dumps(r, default=str) if r else "NONE"))
    c.close()
PYEOF
}

# --- Cleanup mode -----------------------------------------------------------
if [ "$MODE" = "--cleanup" ]; then
  step "Cleanup — removing demo actions from app-writes + production"
  run_py cleanup "$LAKEBASE_PROJECT" "$LAKEBASE_SCHEMA" "$LAKEBASE_DATABASE" "$PROD_BRANCH" "$APP_WRITES_BRANCH" "$DEMO_ID"
  warn "Now run reconcile to propagate the deletes to UC if you added rows via CDF:"
  echo "  databricks bundle run nba_reconcile -t $TARGET"
  exit 0
fi

# --- The demo ---------------------------------------------------------------
echo
printf "${c_bold}NBA Prototype — reconcile demo${c_reset}\n"
echo "  Target: $TARGET   |   Sample action: $DEMO_ID"
echo "  Story: app edit (app-writes) -> Lakebase CDF -> reconcile -> live on production"

step "1/4  Add a sample action on the app-writes branch (simulating an app edit)"
run_py insert "$LAKEBASE_PROJECT" "$LAKEBASE_SCHEMA" "$LAKEBASE_DATABASE" "$PROD_BRANCH" "$APP_WRITES_BRANCH" "$DEMO_ID"
ok "Staged $DEMO_ID on '$APP_WRITES_BRANCH' — production scoring is NOT affected yet."

step "2/4  Wait for Lakebase CDF to capture the change into $CDF_HISTORY_TABLE"
WH="$(databricks warehouses list -o json 2>/dev/null | python3 -c '
import sys,json
d=json.load(sys.stdin)
ws=d if isinstance(d,list) else d.get("warehouses",[])
run=[x for x in ws if str(x.get("state"))=="RUNNING"]
print((run or ws)[0]["id"] if (run or ws) else "")')"
if [ -z "$WH" ]; then
  warn "No SQL warehouse found to poll CDF. Skipping the wait — reconcile will still work."
else
  found=""
  for i in $(seq 1 8); do
    n="$(databricks api post /api/2.0/sql/statements --json "{\"warehouse_id\":\"$WH\",\"statement\":\"SELECT count(*) FROM $CDF_HISTORY_TABLE WHERE action_id='$DEMO_ID'\",\"wait_timeout\":\"30s\"}" 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("result",{}).get("data_array") or [["0"]])[0][0])' 2>/dev/null || echo 0)"
    if [ "$n" != "0" ] && [ -n "$n" ]; then ok "CDF captured $DEMO_ID (history rows: $n)"; found=1; break; fi
    printf "   waiting for CDF… (%s/8)\n" "$i"; sleep 12
  done
  [ -z "$found" ] && warn "Did not see $DEMO_ID in CDF history yet. Is CDF enabled on '$APP_WRITES_BRANCH'? Running reconcile anyway."
fi

step "3/4  Run reconcile (CDF -> UC -> production)"
databricks bundle run nba_reconcile -t "$TARGET"

step "4/4  Verify the action is now LIVE on the production branch"
OUT="$(run_py show_prod "$LAKEBASE_PROJECT" "$LAKEBASE_SCHEMA" "$LAKEBASE_DATABASE" "$PROD_BRANCH" "$APP_WRITES_BRANCH" "$DEMO_ID")"
echo "$OUT" | grep -q 'PRODUCTION_ROW:NONE' \
  && { err "Action did NOT reach production. Check that CDF is enabled and reconcile succeeded."; exit 1; } \
  || ok "Live on production: $(echo "$OUT" | sed 's/.*PRODUCTION_ROW://')"

echo
printf "${c_green}${c_bold}Demo complete.${c_reset} %s flowed app-writes -> CDF -> UC -> production.\n" "$DEMO_ID"
echo "Open the app (Member Lookup) to see it participate in scoring — no model retrain needed."
echo
echo "Clean up the demo rows when done:"
echo "  ./scripts/demo.sh $TARGET --cleanup   &&   databricks bundle run nba_reconcile -t $TARGET"
