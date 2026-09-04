import { AppConfig } from "../lib/api";

function Row({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div className="flex items-center justify-between gap-2 py-0.5 text-xs">
      <span className="text-muted">{label}</span>
      <span className="max-w-[55%] truncate font-mono text-[11px] text-text" title={value}>
        {value}
      </span>
    </div>
  );
}

export function ConfigFooter({ config }: { config?: AppConfig }) {
  if (!config) return null;
  return (
    <div className="border-t border-line px-4 py-3">
      <Row label="Scoring" value={config.branch_production} />
      <Row label="Staging" value={config.branch_app_writes} />
      <Row label="Project" value={config.lakebase_project} />
      <Row label="Schema" value={config.lakebase_schema} />
      <Row label="Model" value={config.model_endpoint_name} />
      {config.genie_enabled && <Row label="Genie" value={config.genie_space_id} />}
      {config.llm_enabled && <Row label="LLM" value={config.llm_endpoint_name} />}
      {config.user && <Row label="User" value={config.user} />}
    </div>
  );
}
