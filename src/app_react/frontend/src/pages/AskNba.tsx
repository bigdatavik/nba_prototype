import { useQuery } from "@tanstack/react-query";
import { useGenie } from "../lib/store";
import { api } from "../lib/api";
import { ChatPanel } from "../components/ChatPanel";
import { Card, Banner } from "../components/ui";
import { Skeleton } from "../components/Skeleton";

export function AskNba() {
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const reference = useQuery({ queryKey: ["reference"], queryFn: api.reference });
  const contextMemberId = useGenie((s) => s.contextMemberId);

  return (
    <div className="flex h-full flex-col space-y-4">
      <div>
        <h1 className="font-display text-2xl font-semibold text-text">Ask NBA</h1>
        <p className="mt-1 text-sm text-muted">
          Natural-language analytics over the book of business — powered by Databricks Genie
          (Unity Catalog + SQL warehouse).
        </p>
      </div>

      {config.isLoading ? (
        <Skeleton className="h-96 w-full" />
      ) : !config.data?.genie_enabled ? (
        <Banner tone="warn">
          Genie is not configured. Set <code>GENIE_SPACE_ID</code> (bundle variable{" "}
          <code>genie_space_id</code>) to enable this page.
        </Banner>
      ) : (
        <Card className="flex h-[72vh] flex-col p-5">
          <div className="min-h-0 flex-1">
            <ChatPanel
              suggestions={reference.data?.ask_nba_suggestions ?? []}
              contextMemberId={contextMemberId}
            />
          </div>
        </Card>
      )}
    </div>
  );
}
