import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { api } from "../lib/api";
import { useGenie } from "../lib/store";
import { Drawer } from "./Drawer";
import { ChatPanel } from "./ChatPanel";

// Cedar-style floating "Ask NBA" pill on every page. Opens a right slide-over
// hosting the same ChatPanel (shared conversation state). Hidden when Genie is
// not configured (genie_enabled=false from /api/config).
export function GenieLauncher() {
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { data: reference } = useQuery({ queryKey: ["reference"], queryFn: api.reference });
  const { open, setOpen, contextMemberId } = useGenie();

  if (!config?.genie_enabled) return null;

  return (
    <>
      {!open && (
        <button
          onClick={() => setOpen(true)}
          className="group fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-full bg-gradient-to-r from-teal to-coral px-4 py-3 text-sm font-semibold text-ink shadow-[0_10px_40px_rgba(45,212,191,0.35)] transition-transform hover:scale-[1.03]"
        >
          <Sparkles size={18} />
          Ask NBA — from question to answer
        </button>
      )}

      <Drawer
        open={open}
        onClose={() => setOpen(false)}
        title={
          <span className="flex items-center gap-2">
            <Sparkles size={18} className="text-teal" /> Ask NBA
          </span>
        }
        subtitle="Natural-language analytics over the book of business · Databricks Genie"
        width="max-w-2xl"
      >
        <ChatPanel
          suggestions={reference?.ask_nba_suggestions ?? []}
          contextMemberId={contextMemberId}
        />
      </Drawer>
    </>
  );
}
