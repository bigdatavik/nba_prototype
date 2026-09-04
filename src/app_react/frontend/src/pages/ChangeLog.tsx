import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { DataTable, Column } from "../components/DataTable";
import { KpiCard } from "../components/KpiCard";
import { Banner } from "../components/ui";
import { Skeleton } from "../components/Skeleton";
import { fmtDate } from "../lib/format";

export function ChangeLog() {
  const changes = useQuery({ queryKey: ["change-log"], queryFn: api.changeLog });
  const rows = changes.data ?? [];

  const columns: Column<Record<string, any>>[] =
    rows.length > 0
      ? Object.keys(rows[0]).map((k) => ({
          key: k,
          header: k.replace(/_/g, " "),
          mono: /score|_at|id|days/.test(k),
          render:
            k === "changed_at"
              ? (r) => fmtDate(r[k])
              : undefined,
        }))
      : [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold text-text">Change Log</h1>
        <p className="mt-1 text-sm text-muted">
          Legacy audit view (Postgres trigger on app-writes). Kept for testing only —
          reconciliation is now driven by Lakebase CDF, not this table.
        </p>
      </div>

      {changes.isLoading ? (
        <Skeleton className="h-56 w-full" />
      ) : rows.length === 0 ? (
        <Banner tone="info">
          No changes recorded yet. Edits made on the Manage Actions page will appear here.
        </Banner>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 sm:max-w-xs">
            <KpiCard label="Total changes" value={rows.length} accent="teal" />
          </div>
          <DataTable columns={columns} rows={rows} />
        </>
      )}
    </div>
  );
}
