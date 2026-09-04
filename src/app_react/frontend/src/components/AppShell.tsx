import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Target, CheckCircle2, SlidersHorizontal, ScrollText, MessageSquare } from "lucide-react";
import clsx from "clsx";
import { api } from "../lib/api";
import { ConfigFooter } from "./ConfigFooter";
import { GenieLauncher } from "./GenieLauncher";

const NAV = [
  { to: "/lookup", label: "Member Lookup", icon: Target },
  { to: "/decisions", label: "Decisions", icon: CheckCircle2 },
  { to: "/actions", label: "Manage Actions", icon: SlidersHorizontal },
  { to: "/change-log", label: "Change Log", icon: ScrollText },
  { to: "/ask", label: "Ask NBA", icon: MessageSquare, genie: true },
];

export function AppShell() {
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });

  return (
    <div className="flex h-screen overflow-hidden bg-ink text-text">
      {/* Sidebar */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-panel">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-teal to-coral text-lg">
            🎯
          </div>
          <div>
            <div className="font-display text-base font-semibold leading-tight text-text">
              NBA Console
            </div>
            <div className="text-[11px] text-muted">Next Best Action</div>
          </div>
        </div>

        <nav className="flex-1 space-y-1 px-3 py-2">
          {NAV.filter((n) => !n.genie || config?.genie_enabled).map((n) => {
            const Icon = n.icon;
            return (
              <NavLink
                key={n.to}
                to={n.to}
                className={({ isActive }) =>
                  clsx(
                    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    isActive
                      ? "bg-panel-2 text-text"
                      : "text-muted hover:bg-panel-2/60 hover:text-text"
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon size={17} className={isActive ? "text-teal" : ""} />
                    {n.label}
                  </>
                )}
              </NavLink>
            );
          })}
        </nav>

        <ConfigFooter config={config} />
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line bg-panel/60 px-6 py-3.5 backdrop-blur">
          <div className="text-sm text-muted">
            Healthcare Payer NBA Engine · Databricks Lakebase + Model Serving
          </div>
          {config?.user && (
            <div className="rounded-full border border-line bg-panel-2 px-3 py-1 text-xs text-muted">
              {config.user}
            </div>
          )}
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-6xl px-6 py-6">
            <Outlet />
          </div>
        </main>
      </div>

      <GenieLauncher />
    </div>
  );
}
