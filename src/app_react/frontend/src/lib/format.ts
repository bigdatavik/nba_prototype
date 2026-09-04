export const fmtScore = (n: number | undefined | null) =>
  n === undefined || n === null ? "—" : n.toFixed(4);

export const fmtPct = (n: number | undefined | null) =>
  n === undefined || n === null ? "—" : `${Math.round(n)}%`;

export const fmt2 = (n: number | undefined | null) =>
  n === undefined || n === null ? "—" : Number(n).toFixed(2);

export const fmt1 = (n: number | undefined | null) =>
  n === undefined || n === null ? "—" : Number(n).toFixed(1);

export const fmtInt = (n: number | string | undefined | null) => {
  if (n === undefined || n === null || n === "") return "—";
  const v = typeof n === "string" ? Number(n) : n;
  if (Number.isNaN(v)) return String(n);
  return v.toLocaleString();
};

export function bandTone(band: string): "good" | "warn" | "fail" | "muted" {
  const b = band.toLowerCase();
  if (b.includes("high")) return "fail";
  if (b.includes("medium")) return "warn";
  if (b.includes("watch")) return "muted";
  return "muted";
}

export function fmtDate(v: string | undefined | null): string {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
