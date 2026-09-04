import { X } from "lucide-react";
import { useEffect } from "react";
import clsx from "clsx";

// Right-side slide-over primitive (the deck/Cedar drawer). Used by the per-member
// AssistDrawer and the global Genie AssistantDrawer.
export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  width = "max-w-xl",
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  width?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50">
      <div
        className="absolute inset-0 animate-fade-in bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      <div
        className={clsx(
          "absolute right-0 top-0 flex h-full w-full flex-col border-l border-line bg-panel shadow-drawer animate-slide-in",
          width
        )}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <div className="font-display text-lg font-semibold text-text">{title}</div>
            {subtitle && <div className="mt-0.5 truncate text-sm text-muted">{subtitle}</div>}
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-muted hover:bg-panel-2 hover:text-text"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}
