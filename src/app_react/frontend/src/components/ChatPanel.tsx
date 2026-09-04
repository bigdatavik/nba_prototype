import { useState, useRef, useEffect } from "react";
import { ChevronDown, ChevronRight, RotateCcw, Send, Sparkles, Loader2 } from "lucide-react";
import { useGenie, GenieTurn } from "../lib/store";
import { AskResponse } from "../lib/api";
import { Button } from "./ui";
import { DataTable, Column } from "./DataTable";
import { Banner } from "./ui";

function ResultTable({ res }: { res: AskResponse }) {
  if (!res.columns || !res.rows || res.rows.length === 0) return null;
  const cols: Column<Record<string, any>>[] = res.columns.map((name, i) => ({
    key: String(i),
    header: name,
    mono: true,
    render: (row) => {
      const v = row[String(i)];
      return v === null || v === undefined ? "—" : String(v);
    },
  }));
  const rows = res.rows.map((r) => {
    const o: Record<string, any> = {};
    r.forEach((v, i) => (o[String(i)] = v));
    return o;
  });
  return (
    <div className="mt-3">
      <DataTable columns={cols} rows={rows} />
    </div>
  );
}

function SqlDisclosure({ sql }: { sql: string }) {
  const [open, setOpen] = useState(false);
  if (!sql) return null;
  return (
    <div className="mt-3 rounded-lg border border-line bg-ink/60">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-medium text-teal hover:text-teal/80"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        View generated SQL
      </button>
      {open && (
        <pre className="overflow-x-auto border-t border-line px-3 py-2 font-mono text-xs leading-relaxed text-muted">
          {sql}
        </pre>
      )}
    </div>
  );
}

function Turn({ turn }: { turn: GenieTurn }) {
  return (
    <div className="space-y-2">
      {/* user bubble */}
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-teal/15 px-3.5 py-2 text-sm text-text">
          {turn.q}
        </div>
      </div>
      {/* assistant bubble */}
      <div className="flex justify-start">
        <div className="max-w-[92%] rounded-2xl rounded-bl-sm border border-line bg-panel-2 px-3.5 py-2.5 text-sm text-text">
          {turn.res === null && !turn.error && (
            <span className="flex items-center gap-2 text-muted">
              <Loader2 size={14} className="animate-spin" /> Asking Genie…
            </span>
          )}
          {turn.error && <Banner tone="fail">{turn.error}</Banner>}
          {turn.res?.error && <Banner tone="fail">{turn.res.error}</Banner>}
          {turn.res && !turn.res.error && (
            <>
              {turn.res.answer ? (
                <div className="whitespace-pre-wrap">{turn.res.answer}</div>
              ) : (
                <div className="text-muted">Query returned results below.</div>
              )}
              <SqlDisclosure sql={turn.res.sql} />
              <ResultTable res={turn.res} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function ChatPanel({
  suggestions,
  contextMemberId,
}: {
  suggestions: string[];
  contextMemberId?: string | null;
}) {
  const { history, loading, ask, reset } = useGenie();
  const [text, setText] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [history, loading]);

  const chips = [...suggestions];
  if (contextMemberId) {
    chips.unshift(
      `How does ${contextMemberId} compare to their market on open care gaps?`
    );
  }

  const submit = () => {
    if (!text.trim()) return;
    ask(text);
    setText("");
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* suggestion chips */}
      <div className="flex flex-wrap gap-2 pb-3">
        {chips.slice(0, 6).map((s, i) => (
          <button
            key={i}
            disabled={loading}
            onClick={() => ask(s)}
            className="rounded-full border border-line bg-panel-2 px-3 py-1.5 text-xs text-muted transition-colors hover:border-teal/50 hover:text-teal disabled:opacity-40"
          >
            {s}
          </button>
        ))}
      </div>

      {/* conversation */}
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
        {history.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-muted">
            <Sparkles className="text-teal" />
            <p className="max-w-xs text-sm">
              Ask a question about members, care gaps, actions, or outcomes. Genie
              queries Unity Catalog directly.
            </p>
          </div>
        )}
        {history.map((t, i) => (
          <Turn key={i} turn={t} />
        ))}
      </div>

      {/* input */}
      <div className="mt-3 border-t border-line pt-3">
        {history.length > 0 && (
          <button
            onClick={reset}
            className="mb-2 flex items-center gap-1.5 text-xs text-muted hover:text-text"
          >
            <RotateCcw size={12} /> New conversation
          </button>
        )}
        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            placeholder="Ask about members, care gaps, actions…"
            className="max-h-32 min-h-[42px] flex-1 resize-none rounded-lg border border-line bg-panel-2 px-3 py-2.5 text-sm text-text placeholder:text-muted/60 focus:border-teal/60 focus:outline-none"
          />
          <Button variant="primary" onClick={submit} disabled={loading || !text.trim()}>
            {loading ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          </Button>
        </div>
      </div>
    </div>
  );
}
