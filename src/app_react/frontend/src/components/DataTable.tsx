import clsx from "clsx";
import React from "react";

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => React.ReactNode;
  align?: "left" | "right" | "center";
  mono?: boolean;
}

export function DataTable<T extends Record<string, any>>({
  columns,
  rows,
  empty = "No rows.",
  rowKey,
}: {
  columns: Column<T>[];
  rows: T[];
  empty?: string;
  rowKey?: (row: T, i: number) => string | number;
}) {
  if (!rows.length) {
    return (
      <div className="rounded-lg border border-dashed border-line px-4 py-10 text-center text-sm text-muted">
        {empty}
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-line">
      <table className="min-w-full border-collapse text-sm">
        <thead>
          <tr className="bg-panel-2">
            {columns.map((c) => (
              <th
                key={c.key}
                className={clsx(
                  "whitespace-nowrap px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-muted",
                  c.align === "right" && "text-right",
                  c.align === "center" && "text-center",
                  (!c.align || c.align === "left") && "text-left"
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={rowKey ? rowKey(row, i) : i}
              className="border-t border-line/70 hover:bg-panel-2/50"
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={clsx(
                    "whitespace-nowrap px-3 py-2.5 text-text",
                    c.align === "right" && "text-right",
                    c.align === "center" && "text-center",
                    c.mono && "font-mono tnum text-[13px]"
                  )}
                >
                  {c.render ? c.render(row) : formatCell(row[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(v: any): React.ReactNode {
  if (v === null || v === undefined || v === "") return <span className="text-muted">—</span>;
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
