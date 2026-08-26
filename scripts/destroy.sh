#!/usr/bin/env bash
# =============================================================================
# FULL teardown of the NBA Prototype — cleans the workspace so you can start fresh.
#
#   ./scripts/destroy.sh <target>              # tear down (default target: dev)
#   ./scripts/destroy.sh azure --yes           # skip the confirmation prompt
#   ./scripts/destroy.sh azure --keep-project  # keep the Lakebase project
#   ./scripts/destroy.sh azure --keep-catalog  # keep the UC catalog entirely
#   ./scripts/destroy.sh azure --drop-catalog  # DROP the whole UC catalog (not just our schemas)
#
# What it removes, in order:
#   1. bundle-managed resources — the app + all jobs (databricks bundle destroy)
#   2. the Lakebase PROJECT — hard-deleted (purged) so its name is immediately
#      reusable, NOT soft-deleted (which would block reusing the name for 7 days).
#   3. Unity Catalog data — by default DROPs the project's schemas:
#        <cdf_catalog>.<cdf_schema>   (CDF history + watermark)
#        <uc_catalog>.<uc_schema>     (source tables, model registrations)
#      With --drop-catalog it DROPs the entire <uc_catalog> instead. Use
#      --keep-catalog to leave UC untouched.
#
# The Lakebase project has `prevent_destroy: true` in resources/nba_lakebase.yml,
# so this script does NOT rely on `bundle destroy` to remove it (that would fail
# on the guard, and destroy only soft-deletes anyway). Instead it purges the
# project directly via the REST API after the bundle resources are gone.
#
# After a teardown, reinstall clean with:  ./scripts/setup.sh <target>
# =============================================================================
source "$(dirname "$0")/_lib.sh"

TARGET="${1:-fevm}"
ASSUME_YES="no"
KEEP_PROJECT="no"
CATALOG_MODE="schemas"   # schemas (default) | catalog | keep
for arg in "$@"; do
  case "$arg" in
    --yes|-y)         ASSUME_YES="yes" ;;
    --keep-project)   KEEP_PROJECT="yes" ;;
    --drop-catalog)   CATALOG_MODE="catalog" ;;
    --keep-catalog)   CATALOG_MODE="keep" ;;
  esac
done

require_cli
load_bundle_config "$TARGET"

cat <<EOF

$(printf "${c_red}${c_bold}")============================================================
  FULL TEARDOWN — this is destructive
============================================================$(printf "${c_reset}")
Target:            $TARGET
Workspace:         $WORKSPACE_HOST
Will DELETE:
  • the app + all jobs (databricks bundle destroy)
$( [ "$KEEP_PROJECT" = "yes" ] \
     && echo "  • (keeping) Lakebase project '$LAKEBASE_PROJECT'" \
     || echo "  • Lakebase project '$LAKEBASE_PROJECT' — PURGED (hard delete, name reusable now)" )
$( case "$CATALOG_MODE" in
     catalog) echo "  • Unity Catalog '$UC_CATALOG' — DROPPED ENTIRELY (all schemas)";;
     keep)    echo "  • (keeping) Unity Catalog data";;
     *)       echo "  • UC schemas '$UC_CATALOG.$UC_SCHEMA' and '$CDF_CATALOG.$CDF_SCHEMA' — DROPPED";;
   esac )
EOF

if [ "$ASSUME_YES" != "yes" ]; then
  printf "\nType 'destroy' to proceed: "
  read -r reply
  [ "$reply" = "destroy" ] || { err "Aborted (you typed '$reply')."; exit 1; }
fi

# --- 1. Remove bundle-managed resources (app + jobs). --------------------------
# The Lakebase project resource carries prevent_destroy:true, so a plain
# `bundle destroy` would fail. We temporarily neutralize the guard so the app +
# jobs can be destroyed, then handle the project explicitly in step 2. The edit
# is reverted afterward so the repo file is left unchanged.
LB_RES="$REPO_ROOT/resources/nba_lakebase.yml"
GUARD_BAK=""
if [ -f "$LB_RES" ] && grep -q "prevent_destroy: true" "$LB_RES"; then
  GUARD_BAK="$(mktemp)"
  cp "$LB_RES" "$GUARD_BAK"
  # comment out the guard line for the duration of the destroy
  sed -i.tmp 's/prevent_destroy: true/prevent_destroy: false  # temporarily disabled by destroy.sh/' "$LB_RES"
  rm -f "$LB_RES.tmp"
fi
restore_guard() { [ -n "$GUARD_BAK" ] && cp "$GUARD_BAK" "$LB_RES" && rm -f "$GUARD_BAK" && ok "Restored prevent_destroy guard in resources/nba_lakebase.yml"; }
trap restore_guard EXIT

step "1/3  Destroying bundle resources (app + jobs)"
databricks bundle destroy -t "$TARGET" --auto-approve \
  || warn "bundle destroy reported an issue; continuing to project cleanup."

# --- 2. Purge the Lakebase project (unless --keep-project). --------------------
if [ "$KEEP_PROJECT" = "yes" ]; then
  ok "2/3  Keeping Lakebase project '$LAKEBASE_PROJECT' (per --keep-project)."
else
  step "2/3  Purging Lakebase project '$LAKEBASE_PROJECT' (hard delete)"
  # NOTE: the CLI `postgres delete-project` verb only soft-deletes (7-day
  # retention, blocks reusing the name). The REST API with ?purge=true hard
  # deletes immediately so the name is reusable right away.
  if databricks api delete "/api/2.0/postgres/projects/${LAKEBASE_PROJECT}?purge=true" >/dev/null 2>&1; then
    ok "Purged project '$LAKEBASE_PROJECT' — the name is free to reuse now."
  else
    warn "Could not purge via API (already gone, or older workspace). Falling back to CLI soft-delete:"
    databricks postgres delete-project "projects/${LAKEBASE_PROJECT}" \
      || warn "Project delete failed — remove it from the Lakebase UI if it lingers."
    warn "Soft-deleted: the name '$LAKEBASE_PROJECT' may be blocked for ~7 days."
  fi
fi

# --- 3. Drop Unity Catalog data. -----------------------------------------------
# Default: drop just this project's schemas (source + CDF). --drop-catalog drops
# the whole catalog; --keep-catalog skips this step. Uses a SQL warehouse.
if [ "$CATALOG_MODE" = "keep" ]; then
  ok "3/3  Keeping Unity Catalog data (per --keep-catalog)."
else
  step "3/3  Dropping Unity Catalog data"
  WID="$(find_warehouse)"
  if [ -z "$WID" ]; then
    warn "No SQL warehouse available to drop UC objects. Drop them manually:"
    if [ "$CATALOG_MODE" = "catalog" ]; then
      warn "  DROP CATALOG IF EXISTS ${UC_CATALOG} CASCADE;"
    else
      warn "  DROP SCHEMA IF EXISTS ${CDF_CATALOG}.${CDF_SCHEMA} CASCADE;"
      warn "  DROP SCHEMA IF EXISTS ${UC_CATALOG}.${UC_SCHEMA} CASCADE;"
    fi
  elif [ "$CATALOG_MODE" = "catalog" ]; then
    if [ "$(run_sql "$WID" "DROP CATALOG IF EXISTS ${UC_CATALOG} CASCADE")" = "SUCCEEDED" ]; then
      ok "Dropped catalog ${UC_CATALOG} (all schemas)."
    else
      warn "Could not drop catalog ${UC_CATALOG} — drop it manually if it lingers."
    fi
  else
    # Drop the two project schemas. CDF schema may equal the UC catalog but a
    # different schema; drop both. IF EXISTS keeps this safe/idempotent.
    for fq in "${CDF_CATALOG}.${CDF_SCHEMA}" "${UC_CATALOG}.${UC_SCHEMA}"; do
      if [ "$(run_sql "$WID" "DROP SCHEMA IF EXISTS ${fq} CASCADE")" = "SUCCEEDED" ]; then
        ok "Dropped schema ${fq}."
      else
        warn "Could not drop schema ${fq} — drop it manually if it lingers."
      fi
    done
  fi
fi

echo
ok "Teardown complete."
echo "Reinstall clean with:  ./scripts/setup.sh $TARGET"
echo
warn "Heads-up: the databricksapps DNS zone has a 24h negative-cache TTL. If you"
warn "recreate the app soon and the browser says 'site can't be reached'"
warn "(ERR_NAME_NOT_RESOLVED) while the Apps UI shows it Running, clear your DNS"
warn "cache (Chrome chrome://net-internals/#dns, or macOS dscacheutil -flushcache,"
warn "or use 8.8.8.8). The app is fine; it's stale local DNS."
